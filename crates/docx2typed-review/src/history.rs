//! Generation-bound review history (issue #60 review frame contract): the
//! append-only `.review/history.jsonl` trail is a sequence of records, each
//! bound to the immutable store generation it was recorded against.
//!
//! Every record carries an opaque `history_id` (frontend-safe: never the
//! generation id), the generation reference, and the session coordinates
//! (`current_snapshot` / `review_base` / `origin` / `recorded_at`).
//! Multiple generations may map to the same C snapshot (store generations
//! advance on every mutation; the C snapshot only advances on publish).
//!
//! The referenced generation stays a live reference (no GC in this slice):
//! the frame reconstruction (`GET /api/review-frame?history=<history_id>`)
//! reads the read model from that generation's directory. The manifest hash
//! is a deterministic function of the generation, so records written inside
//! a mutation closure (before the store has computed the final manifest)
//! are enriched at serve time from the immutable generation manifests.
//!
//! Legacy records written before this contract carry no `history_id` /
//! `generation`; they are served with a deterministic derived id and a null
//! generation (they predate atomic frames and cannot be reconstructed as
//! one — the reader fails closed instead of guessing).

use std::fs;
use std::path::{Path, PathBuf};

use docx2typed_protocol::{bytes_sha256, new_operation_id, utc_now_iso};
use serde_json::{json, Value};

pub const HISTORY_SCHEMA: &str = "docx2typed-review-history-1";
pub const HISTORY_LIST_SCHEMA: &str = "docx2typed-review-history-list-1";

fn history_path(workdir: &Path) -> PathBuf {
    workdir.join(".review").join("history.jsonl")
}

/// The store generation this path is a live copy of, when determinable.
///
/// - Inside a store mutation closure the path IS a generation directory
///   (`.docx2typed-store/generations/<gen>`): the directory name is the
///   generation id, and the manifest is not final yet (the store computes it
///   after the closure returns) — the caller records a null manifest and the
///   reader enriches it from the immutable generation manifest.
/// - At a store-backed workdir root the pointer selects the current
///   generation; the manifest is the pointer's manifest.
/// - Legacy non-store workdirs have no generation.
pub fn generation_binding(workdir: &Path) -> (Option<String>, Option<String>) {
    // A generation copy lives at <root>/.docx2typed-store/generations/<gen>:
    // its parent's parent is the store dir that contains `generations`.
    let is_generation_copy = workdir
        .parent()
        .and_then(|parent| parent.parent())
        .map(|store_dir| {
            store_dir
                .file_name()
                .map(|name| name == ".docx2typed-store")
                .unwrap_or(false)
                && store_dir.join("generations").is_dir()
        })
        .unwrap_or(false);
    if is_generation_copy {
        (
            workdir
                .file_name()
                .map(|name| name.to_string_lossy().into_owned()),
            None,
        )
    } else if docx2typed_store::has_store(workdir) {
        match docx2typed_store::Store::new(workdir).pin() {
            Ok(pin) => (Some(pin.generation), pin.manifest_sha256),
            Err(_) => (None, None),
        }
    } else {
        (None, None)
    }
}

/// Deterministic opaque id for a legacy record (written before the
/// generation-bound contract): derived from the record content so re-reads
/// always agree.
fn legacy_history_id(record: &Value) -> String {
    let mut bytes = serde_json::to_vec(record).unwrap_or_default();
    bytes.push(b'\n');
    format!("legacy-{}", &bytes_sha256(&bytes)[..24])
}

/// Normalize one raw history line into a served record: schema, opaque
/// history_id, generation reference, session coordinates, recorded_at, and
/// the event payload. `resolve_manifest` supplies the authoritative
/// generation manifest hash (a deterministic function of the generation).
fn normalize(record: &Value, resolve_manifest: &dyn Fn(&str) -> Option<String>) -> Value {
    let mut out = record.clone();
    let legacy_id = legacy_history_id(&out);
    let obj = out
        .as_object_mut()
        .expect("history records are JSON objects");
    obj.insert("schema".to_string(), json!(HISTORY_SCHEMA));
    let has_id = obj
        .get("history_id")
        .and_then(Value::as_str)
        .map(|id| !id.is_empty())
        .unwrap_or(false);
    if !has_id {
        obj.insert("history_id".to_string(), json!(legacy_id));
    }
    if !obj.contains_key("generation") {
        obj.insert("generation".to_string(), Value::Null);
    }
    let generation = obj
        .get("generation")
        .and_then(Value::as_str)
        .map(str::to_string);
    let manifest = generation
        .as_deref()
        .and_then(resolve_manifest)
        .map(Value::String)
        .unwrap_or(Value::Null);
    obj.insert("generation_manifest_sha256".to_string(), manifest);
    for key in ["current_snapshot", "review_base", "origin", "recorded_at"] {
        if !obj.contains_key(key) {
            obj.insert(key.to_string(), Value::Null);
        }
    }
    out
}

/// Append one generation-bound history record. `generation` / `manifest`
/// name the store generation the record is recorded against (null for
/// legacy non-store workdirs). Session coordinates ride on the event
/// payload when present. Returns the served record.
pub fn append(
    workdir: &Path,
    event: &Value,
    generation: Option<&str>,
    manifest: Option<&str>,
) -> Value {
    let mut record = json!({
        "schema": HISTORY_SCHEMA,
        "history_id": new_operation_id(),
        "generation": generation.map(|g| Value::String(g.to_string())).unwrap_or(Value::Null),
        "generation_manifest_sha256": manifest.map(|m| Value::String(m.to_string())).unwrap_or(Value::Null),
        "recorded_at": utc_now_iso(),
        "current_snapshot": Value::Null,
        "review_base": Value::Null,
        "origin": Value::Null,
    });
    for (key, value) in event.as_object().unwrap_or(&serde_json::Map::new()) {
        record[key] = value.clone();
    }
    let mut bytes = serde_json::to_vec(&record).expect("history serializes");
    bytes.push(b'\n');
    let _ = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(history_path(workdir))
        .and_then(|mut handle| std::io::Write::write_all(&mut handle, &bytes));
    record
}

/// Read every history record (side-effect free), normalized and enriched:
/// `resolve_manifest` maps each record's referenced generation to its
/// authoritative manifest hash. Missing/corrupt lines are skipped (the
/// append-only trail tolerates torn tails; the served schema is canonical).
pub fn list(workdir: &Path, resolve_manifest: &dyn Fn(&str) -> Option<String>) -> Value {
    let mut records = Vec::new();
    let Ok(text) = fs::read_to_string(history_path(workdir)) else {
        return json!({ "schema": HISTORY_LIST_SCHEMA, "records": records });
    };
    for line in text.lines() {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if value.is_object() {
            records.push(normalize(&value, resolve_manifest));
        }
    }
    json!({ "schema": HISTORY_LIST_SCHEMA, "records": records })
}

/// Read one history record by its opaque history id (side-effect free),
/// normalized and enriched like `list`. `None` when the id is unknown.
pub fn read(
    workdir: &Path,
    history_id: &str,
    resolve_manifest: &dyn Fn(&str) -> Option<String>,
) -> Option<Value> {
    let Ok(text) = fs::read_to_string(history_path(workdir)) else {
        return None;
    };
    for line in text.lines() {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let normalized = normalize(&value, resolve_manifest);
        if normalized.get("history_id").and_then(Value::as_str) == Some(history_id) {
            return Some(normalized);
        }
    }
    None
}
