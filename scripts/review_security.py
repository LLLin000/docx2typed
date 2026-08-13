"""Issue #31 transport-security primitives for the single-session review HTTP
surface.

One process-scoped 256-bit capability (OS CSPRNG, base64url without padding)
held only in server memory, compared in constant time, and revoked by process
termination.  The Host allowlist covers exactly the advertised loopback or
Tailscale IPv4 origin; browser-origin gates block cross-origin blind writes;
a small bounded in-process token bucket throttles unauthorized requests
without ever rate-limiting authorized interaction.  Nothing here is
persisted, rotated, or shared across processes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

_CAPABILITY_BYTES = 32  # 256 bits
_SESSION_HASH_LEN = 10  # hex chars: irreversible truncated session hash

# Detail-free bodies are byte-identical for every authority failure so a
# probe cannot distinguish a bad token, a bad Host, a throttled client, or an
# unknown route.
NOT_FOUND_BODY = {"error": "not-found"}

# Strict self-only CSP: no third-party resources, frame denial via CSP plus
# X-Frame-Options. Inline script/style are required because the console page
# is a single server-generated document (no external assets at all).
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def generate_capability() -> str:
    """One fresh 256-bit capability, base64url encoded without padding."""
    return base64.urlsafe_b64encode(os.urandom(_CAPABILITY_BYTES)).rstrip(b"=").decode("ascii")


def constant_time_equal(left: str, right: str) -> bool:
    """Compare two tokens without early-exit timing leakage."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def session_fingerprint(token: str) -> str:
    """Irreversible truncated hash of one presented token, for log
    correlation only. Reveals nothing about the capability itself."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:_SESSION_HASH_LEN]


def split_host(host_header: str) -> tuple[str, int | None]:
    """Split a Host header into (host, port). Port is None when omitted.

    IPv6 literals are rejected outright: the contract binds only to loopback
    IPv4 or one auto-discovered Tailscale IPv4.
    """
    value = host_header.strip()
    if not value or value.startswith("["):
        raise ValueError("invalid host")
    if ":" in value:
        host, _, port = value.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("invalid host")
        return host, int(port)
    return value, None


def build_allowlist(bind_host: str, port: int) -> tuple[frozenset[str], frozenset[str]]:
    """Host-header allowlist and advertised-origin set for one bind.

    Loopback accepts both the ``127.0.0.1`` and ``localhost`` forms (the
    console may be opened through either); a Tailscale bind accepts exactly
    the discovered IPv4 address.  Ports match the bound port; the port-less
    form is admitted only for port 80 where browsers omit it.
    """
    hosts: set[str] = set()
    origins: set[str] = set()
    names = ("127.0.0.1", "localhost") if bind_host in ("127.0.0.1", "localhost") else (bind_host,)
    for name in names:
        hosts.add(f"{name}:{port}")
        origins.add(f"http://{name}:{port}")
        if port == 80:
            hosts.add(name)
            origins.add(f"http://{name}")
    return frozenset(hosts), frozenset(origins)


def origin_matches(origin: str, allowed_origins: frozenset[str]) -> bool:
    """True when the Origin header equals an advertised origin (scheme, host,
    and port all exact).``"""
    return origin in allowed_origins


_CONTENT_TYPE_RE = re.compile(r"^application/json(?:\s*;\s*charset\s*=\s*utf-?8)?$", re.IGNORECASE)


def content_type_allowed(value: str) -> bool:
    """Body-bearing writes accept only ``application/json`` with an optional
    UTF-8 charset.  text/plain, forms, multipart, and everything else fail."""
    return bool(value) and _CONTENT_TYPE_RE.match(value.strip()) is not None


@dataclass
class _Bucket:
    tokens: float
    updated: float


class UnauthorizedThrottle:
    """Bounded in-process per-client token bucket for unauthorized requests.

    Authorized requests never consult the bucket.  Memory stays flat under a
    flood: the client table is capped and evicts the least recently seen
    entry; nothing is persisted.  A client that exhausts its capacity is
    refused (429) until the bucket refills.
    """

    def __init__(
        self,
        capacity: float = 10.0,
        refill_per_second: float = 1.0,
        max_clients: int = 128,
    ) -> None:
        self.capacity = capacity
        self.refill = refill_per_second
        self.max_clients = max_clients
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(client)
            if bucket is None:
                if len(self._buckets) >= self.max_clients:
                    self._buckets.popitem(last=False)
                bucket = _Bucket(self.capacity, now)
                self._buckets[client] = bucket
            bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.refill)
            bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)


@dataclass(frozen=True)
class ReviewSecurity:
    """Immutable per-process security context for one review session."""

    capability: str
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    port: int
    source_label: str
    session_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_hash", session_fingerprint(self.capability))

    def host_allowed(self, host_header: str) -> bool:
        try:
            host, _port = split_host(host_header)
        except ValueError:
            return False
        return host_header.strip() in self.allowed_hosts

    def origin_allowed(self, origin: str) -> bool:
        return origin_matches(origin, self.allowed_origins)

    def verify(self, token: str) -> bool:
        """Constant-time capability verification."""
        return bool(token) and constant_time_equal(token, self.capability)
