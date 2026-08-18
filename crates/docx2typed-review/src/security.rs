//! Issue #31 transport-security primitives for the single-session review
//! HTTP surface — mirror of `scripts/review_security.py`.
//!
//! One process-scoped 256-bit capability (OS CSPRNG, base64url without
//! padding) held only in server memory, compared in constant time, and
//! revoked by process termination. The Host allowlist covers exactly the
//! advertised loopback or Tailscale IPv4 origin; browser-origin gates block
//! cross-origin blind writes; a small bounded in-process token bucket
//! throttles unauthorized requests without ever rate-limiting authorized
//! interaction. Nothing here is persisted, rotated, or shared across
//! processes.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

/// 256 bits.
const CAPABILITY_BYTES: usize = 32;
/// hex chars: irreversible truncated session hash.
const SESSION_HASH_LEN: usize = 10;

/// Detail-free bodies are byte-identical for every authority failure so a
/// probe cannot distinguish a bad token, a bad Host, a throttled client, or
/// an unknown route.
pub const NOT_FOUND_BODY: &str = r#"{"error":"not-found"}"#;

/// Strict self-only CSP: no third-party resources, no inline script/style
/// (script-src/style-src are `'self'` — the console page must be served as
/// external assets or none), frame denial via CSP plus X-Frame-Options, and
/// no base-uri or form-action targets.
pub const CONTENT_SECURITY_POLICY: &str = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'";

/// A fresh 256-bit capability, base64url encoded without padding.
pub fn generate_capability() -> String {
    let mut bytes = [0u8; CAPABILITY_BYTES];
    getrandom::getrandom(&mut bytes).expect("OS CSPRNG available");
    base64url(&bytes)
}

/// Base64url without padding (no external base64 crate needed).
fn base64url(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(triple >> 18) as usize & 63] as char);
        out.push(ALPHABET[(triple >> 12) as usize & 63] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[(triple >> 6) as usize & 63] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[triple as usize & 63] as char);
        }
    }
    out
}

/// Compare two tokens without early-exit timing leakage.
pub fn constant_time_equal(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0u8;
    for (a, b) in left.iter().zip(right.iter()) {
        diff |= a ^ b;
    }
    diff == 0
}

/// Irreversible truncated hash of one presented token, for log correlation
/// only. Reveals nothing about the capability itself.
pub fn session_fingerprint(token: &str) -> String {
    let digest = Sha256::digest(token.as_bytes());
    hex::encode(digest)[..SESSION_HASH_LEN].to_string()
}

/// Split a Host header into (host, port). Port is `None` when omitted.
/// IPv6 literals are rejected outright: the contract binds only to loopback
/// IPv4 or one auto-discovered Tailscale IPv4.
pub fn split_host(host_header: &str) -> Result<(String, Option<u16>), String> {
    let value = host_header.trim();
    if value.is_empty() || value.starts_with('[') {
        return Err("invalid host".to_string());
    }
    if let Some((host, port)) = value.rsplit_once(':') {
        if host.is_empty() || !port.chars().all(|ch| ch.is_ascii_digit()) {
            return Err("invalid host".to_string());
        }
        let port: u16 = port.parse().map_err(|_| "invalid host".to_string())?;
        return Ok((host.to_string(), Some(port)));
    }
    Ok((value.to_string(), None))
}

/// Host-header allowlist and advertised-origin set for one bind.
///
/// Loopback accepts both the `127.0.0.1` and `localhost` forms; a Tailscale
/// bind accepts exactly the discovered IPv4 address. Ports match the bound
/// port; the port-less form is admitted only for port 80 where browsers
/// omit it.
pub fn build_allowlist(bind_host: &str, port: u16) -> (Vec<String>, Vec<String>) {
    let mut hosts = Vec::new();
    let mut origins = Vec::new();
    let names: &[&str] = if bind_host == "127.0.0.1" || bind_host == "localhost" {
        &["127.0.0.1", "localhost"]
    } else {
        std::slice::from_ref(&bind_host)
    };
    for name in names {
        hosts.push(format!("{name}:{port}"));
        origins.push(format!("http://{name}:{port}"));
        if port == 80 {
            hosts.push(name.to_string());
            origins.push(format!("http://{name}"));
        }
    }
    (hosts, origins)
}

/// True when the Origin header equals an advertised origin (scheme, host,
/// and port all exact).
pub fn origin_matches(origin: &str, allowed_origins: &[String]) -> bool {
    allowed_origins.iter().any(|allowed| allowed == origin)
}

/// Body-bearing writes accept only `application/json` with an optional
/// UTF-8 charset. text/plain, forms, multipart, and everything else fail.
pub fn content_type_allowed(value: &str) -> bool {
    let value = value.trim();
    if value.is_empty() {
        return false;
    }
    let (mime, params) = match value.split_once(';') {
        Some((mime, params)) => (mime.trim(), params.trim()),
        None => (value, ""),
    };
    if !mime.eq_ignore_ascii_case("application/json") {
        return false;
    }
    if params.trim().is_empty() {
        return true;
    }
    // Exactly one optional `; charset=utf-8` (or `utf8`) parameter, matching
    // the Python mirror `(?:\s*;\s*charset\s*=\s*utf-?8)?$` — case
    // insensitive, whitespace-tolerant around `;` and `=`, never quoted.
    let params = params.trim().to_ascii_lowercase();
    let value = params.strip_prefix("charset").unwrap_or("").trim_start();
    let value = value.strip_prefix('=').unwrap_or(value).trim();
    matches!(value, "utf-8" | "utf8")
}

/// Bounded in-process per-client token bucket for unauthorized requests.
///
/// Authorized requests never consult the bucket. Memory stays flat under a
/// flood: the client table is capped and evicts the least recently seen
/// entry; nothing is persisted. A client that exhausts its capacity is
/// refused (429) until the bucket refills.
pub struct UnauthorizedThrottle {
    capacity: f64,
    refill_per_second: f64,
    max_clients: usize,
    buckets: HashMap<String, Bucket>,
}

struct Bucket {
    tokens: f64,
    updated: Instant,
}

impl UnauthorizedThrottle {
    pub fn new(capacity: f64, refill_per_second: f64, max_clients: usize) -> Self {
        UnauthorizedThrottle {
            capacity,
            refill_per_second,
            max_clients,
            buckets: HashMap::new(),
        }
    }

    pub fn allow(&mut self, client: &str) -> bool {
        let now = Instant::now();
        let bucket = match self.buckets.get_mut(client) {
            Some(bucket) => bucket,
            None => {
                if self.buckets.len() >= self.max_clients {
                    // Evict the least recently seen entry.
                    if let Some(oldest) = self
                        .buckets
                        .iter()
                        .min_by_key(|(_, bucket)| bucket.updated)
                        .map(|(client, _)| client.clone())
                    {
                        self.buckets.remove(&oldest);
                    }
                }
                self.buckets.insert(
                    client.to_string(),
                    Bucket {
                        tokens: self.capacity,
                        updated: now,
                    },
                );
                self.buckets.get_mut(client).expect("just inserted")
            }
        };
        let elapsed = now.duration_since(bucket.updated).as_secs_f64();
        bucket.tokens = (self.capacity).min(bucket.tokens + elapsed * self.refill_per_second);
        bucket.updated = now;
        if bucket.tokens >= 1.0 {
            bucket.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    pub fn len(&self) -> usize {
        self.buckets.len()
    }

    pub fn is_empty(&self) -> bool {
        self.buckets.is_empty()
    }

    /// Token bucket idle time used by tests to prove refill.
    pub fn refill_per_second(&self) -> f64 {
        self.refill_per_second
    }

    pub fn capacity(&self) -> f64 {
        self.capacity
    }
}

/// Immutable per-process security context for one review session.
pub struct ReviewSecurity {
    pub capability: String,
    pub allowed_hosts: Vec<String>,
    pub allowed_origins: Vec<String>,
    pub port: u16,
    pub source_label: String,
    pub session_hash: String,
}

impl ReviewSecurity {
    pub fn new(
        capability: String,
        allowed_hosts: Vec<String>,
        allowed_origins: Vec<String>,
        port: u16,
        source_label: String,
    ) -> Self {
        let session_hash = session_fingerprint(&capability);
        ReviewSecurity {
            capability,
            allowed_hosts,
            allowed_origins,
            port,
            source_label,
            session_hash,
        }
    }

    pub fn host_allowed(&self, host_header: &str) -> bool {
        if split_host(host_header).is_err() {
            return false;
        }
        self.allowed_hosts
            .iter()
            .any(|allowed| allowed == host_header.trim())
    }

    pub fn origin_allowed(&self, origin: &str) -> bool {
        origin_matches(origin, &self.allowed_origins)
    }

    /// Constant-time capability verification.
    pub fn verify(&self, token: &str) -> bool {
        !token.is_empty() && constant_time_equal(token, &self.capability)
    }
}

/// Default idle threshold used to prove refill in tests (1 second).
pub fn throttle_idle_after() -> Duration {
    Duration::from_secs(1)
}
