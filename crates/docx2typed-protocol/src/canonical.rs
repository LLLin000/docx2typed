//! Canonical JSON + SHA-256 primitives, mirroring `scripts/protocol.py`
//! (`_canonical_bytes`/`semantic_sha256`/`file_sha256`/`new_operation_id`).
//!
//! Python canonical form: `json.dumps(v, ensure_ascii=False, sort_keys=True,
//! separators=(",", ":"))`. serde_json's default `Value` map is a
//! `BTreeMap`, so `serde_json::to_string` emits sorted keys compact with no
//! ASCII escaping — the same bytes for the object/array/string/number/bool
//! values this protocol carries.

use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

/// Canonical JSON text of a value (sorted keys, compact separators).
pub fn canonical_json(value: &Value) -> String {
    serde_json::to_string(value).expect("serde_json::Value always serializes")
}

/// Python-compatible semantic SHA-256 of a JSON value.
pub fn semantic_sha256(value: &Value) -> String {
    hex::encode(Sha256::digest(canonical_json(value).as_bytes()))
}

pub fn bytes_sha256(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

pub fn file_sha256(path: &Path) -> std::io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 1024 * 1024];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// Caller-visible operation identity: UUID v4 hex (never derived from
/// inputs), mirroring `new_operation_id()`.
pub fn new_operation_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// UTC ISO-8601 with seconds precision and `+00:00` offset, mirroring
/// Python `datetime.now(timezone.utc).isoformat(timespec="seconds")` (used
/// for synthesized revision dates, issue #59).
pub fn utc_now_iso() -> String {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let (year, month, day) = civil_from_days((seconds / 86_400) as i64);
    let hms = seconds % 86_400;
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00",
        year,
        month,
        day,
        hms / 3600,
        (hms % 3600) / 60,
        hms % 60
    )
}

/// Days since 1970-01-01 to civil date (Howard Hinnant's algorithm).
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Stable identity of one operation attempt (operation name + canonical
/// argument payload); time/host/run identity never participate.
pub fn canonical_operation_input(operation: &str, args: &Value) -> String {
    semantic_sha256(&serde_json::json!({"operation": operation, "args": args}))
}

/// Strip the Windows `\\?\` verbatim prefix that `std::fs::canonicalize`
/// adds, so resolved paths match Python `Path.resolve()` output
/// (`C:\Users\...`, backslashes intact, no prefix).
pub fn strip_verbatim_prefix(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        PathBuf::from(rest)
    } else {
        path
    }
}

/// `{"kind": "absolute", "value": <resolved path>}` — mirror of Python's
/// `typed_path`.
pub fn typed_path_value(path: &Path) -> Value {
    serde_json::json!({
        "kind": "absolute",
        "value": resolve_path(path).to_string_lossy(),
    })
}

/// Python-compatible non-strict absolute path resolution (symlinks in the
/// existing ancestor chain resolved, non-existing tail kept verbatim —
/// mirroring Python `Path.resolve(strict=False)`).
///
/// A two-branch implementation (canonicalize when it exists, plain absolute
/// otherwise) would make the resolved form depend on whether a path exists,
/// so the same operation with a not-yet-published output would canonicalize
/// differently on retry after publication and break operation-id replay.
/// Always resolve the existing prefix the same way, regardless of whether
/// the final component exists yet.
pub fn resolve_path(path: &Path) -> PathBuf {
    let absolute = std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
    // Full path exists: OS-canonicalize (resolves symlinks/case) — same as
    // Python resolve() on an existing path.
    if let Ok(canonical) = std::fs::canonicalize(&absolute) {
        return strip_verbatim_prefix(canonical);
    }
    // Non-existing tail: canonicalize the deepest existing ancestor and
    // re-append the missing components verbatim. This makes the resolved
    // form independent of whether the final component exists yet, so the
    // same operation retried after publication canonicalizes identically
    // (operation-id replay safety).
    let mut current = absolute.clone();
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(canonical) = std::fs::canonicalize(&current) {
            let mut out = canonical.into_os_string();
            for component in tail.iter().rev() {
                out.push(std::path::MAIN_SEPARATOR_STR);
                out.push(component);
            }
            return strip_verbatim_prefix(PathBuf::from(out));
        }
        match current.file_name() {
            Some(name) => {
                tail.push(name.to_os_string());
                match current.parent() {
                    Some(parent) => current = parent.to_path_buf(),
                    None => break,
                }
            }
            None => break,
        }
    }
    strip_verbatim_prefix(absolute)
}
