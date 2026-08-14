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

/// Python-compatible absolute path resolution (symlinks resolved, no
/// verbatim prefix). Falls back to a plain absolute path when the path does
/// not exist (Python `resolve()` is non-strict by default).
pub fn resolve_path(path: &Path) -> PathBuf {
    match std::fs::canonicalize(path) {
        Ok(absolute) => strip_verbatim_prefix(absolute),
        Err(_) => {
            strip_verbatim_prefix(std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf()))
        }
    }
}
