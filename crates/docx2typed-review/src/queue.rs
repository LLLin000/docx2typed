//! File-backed review events shared by the browser server and MCP agent —
//! mirror of `scripts/review_queue.py`.
//!
//! Events live under `<workdir>/.review/inbox/<event_id>.json` with schema
//! `docx2typed-review-event-1`. Types: `decision`, `comment`, `patch`;
//! statuses `draft`/`queued`/`acknowledged`; delivery states
//! `staged`/`queued`/`in_progress`/`applied`/`acknowledged`; review
//! decisions `pending`/`accept`/`reject`/`defer`/`adjusted`/`comment`.
//! Writes are atomic (temp + rename); serialization across processes comes
//! from the surrounding store Writer lane (review mutations run inside
//! `Store::mutate`), so no separate queue lock is needed here.

use std::fs;
use std::path::{Path, PathBuf};

use docx2typed_protocol::new_operation_id;
use serde_json::{json, Value};

pub const QUEUE_DIR: &str = ".review/inbox";
pub const SCHEMA: &str = "docx2typed-review-event-1";
const ALLOWED_TYPES: [&str; 2] = ["decision", "comment"];
const ALLOWED_STATUSES: [&str; 3] = ["draft", "queued", "acknowledged"];
const ALLOWED_DELIVERY_STATES: [&str; 5] =
    ["staged", "queued", "in_progress", "applied", "acknowledged"];
const ALLOWED_REVIEW_DECISIONS: [&str; 6] = [
    "pending", "accept", "reject", "defer", "adjusted", "comment",
];
const MAX_TEXT: usize = 8_000;

fn now() -> String {
    docx2typed_protocol::utc_now_iso()
}

fn root(workdir: &Path) -> PathBuf {
    let path = workdir.join(QUEUE_DIR);
    let _ = fs::create_dir_all(&path);
    path
}

fn event_path(workdir: &Path, event_id: &str) -> Result<PathBuf, String> {
    if event_id.is_empty()
        || !event_id
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '-' || ch == '_')
    {
        return Err("invalid event_id".to_string());
    }
    Ok(root(workdir).join(format!("{event_id}.json")))
}

fn atomic_write(path: &Path, data: &Value) -> Result<(), String> {
    let parent = path.parent().ok_or("event path has no parent")?;
    let temp = temp_path(parent, path);
    let result = (|| -> Result<(), String> {
        let mut bytes = serde_json::to_vec(data).map_err(|error| error.to_string())?;
        bytes.push(b'\n');
        fs::write(&temp, &bytes).map_err(|error| error.to_string())?;
        fs::rename(&temp, path).map_err(|error| error.to_string())?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

fn temp_path(parent: &Path, target: &Path) -> PathBuf {
    let name = target
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "event".to_string());
    parent.join(format!(".{name}-{}.tmp", &new_operation_id()[..8]))
}

fn bounded(value: Option<&Value>, name: &str, limit: usize) -> Result<String, String> {
    let text = match value {
        None => String::new(),
        Some(value) => value.as_str().map(str::to_string).unwrap_or_default(),
    };
    let text = text.trim().to_string();
    if text.len() > limit {
        return Err(format!("{name} is too long"));
    }
    Ok(text)
}

fn read(path: &Path) -> Option<Value> {
    let bytes = fs::read(path).ok()?;
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    if value.is_object() {
        Some(value)
    } else {
        None
    }
}

/// Validate + normalize one decision/comment event (patch events go through
/// `collab::validate_patch`).
pub fn validate_event(event: &Value) -> Result<Value, String> {
    if event.get("type").and_then(Value::as_str) == Some("patch") {
        return crate::collab::validate_patch(event).map_err(|error| error.detail);
    }
    let event_type = bounded(event.get("type"), "type", 32)?;
    if !ALLOWED_TYPES.contains(&event_type.as_str()) {
        return Err("type must be decision or comment".to_string());
    }
    let mut normalized = event.clone();
    normalized["type"] = json!(event_type);
    let client_id = bounded(event.get("client_id"), "client_id", 200)?;
    if client_id.is_empty() {
        return Err("client_id is required".to_string());
    }
    normalized["client_id"] = json!(client_id);
    let review_item_id = bounded(event.get("review_item_id").or(None), "review_item_id", 300)?;
    normalized["review_item_id"] = json!(if review_item_id.is_empty() {
        format!("{event_type}:{client_id}")
    } else {
        review_item_id
    });
    let paragraph_id = bounded(event.get("paragraph_id"), "paragraph_id", 120)?;
    normalized["paragraph_id"] = json!(paragraph_id);
    if event_type == "decision" {
        let decision = bounded(event.get("decision"), "decision", 32)?;
        if !matches!(decision.as_str(), "accept" | "reject" | "defer" | "comment") {
            return Err("decision must be accept, reject, defer, or comment".to_string());
        }
        normalized["decision"] = json!(decision);
        normalized["review_decision"] = json!(decision);
        normalized["revision_key"] =
            json!(bounded(event.get("revision_key"), "revision_key", 500)?);
        normalized["revision_id"] = json!(bounded(event.get("revision_id"), "revision_id", 120)?);
        normalized["selected_text"] = json!(bounded(
            event.get("selected_text"),
            "selected_text",
            MAX_TEXT
        )?);
        normalized["comment"] = json!(bounded(event.get("comment"), "comment", MAX_TEXT)?);
    } else {
        normalized["review_decision"] = json!("pending");
        let selected_text = bounded(event.get("selected_text"), "selected_text", MAX_TEXT)?;
        let note = bounded(event.get("note"), "note", MAX_TEXT)?;
        normalized["selected_text"] = json!(selected_text);
        normalized["note"] = json!(note);
        if selected_text.is_empty() || note.is_empty() {
            return Err("comment requires selected_text and note".to_string());
        }
        normalized["before_context"] = json!(bounded(
            event.get("before_context"),
            "before_context",
            2_000
        )?);
        normalized["after_context"] =
            json!(bounded(event.get("after_context"), "after_context", 2_000)?);
    }
    normalized["delivery_state"] = json!("staged");
    Ok(normalized)
}

/// Read every valid event, sorted by (created_at, event_id). Creates the
/// inbox directory (mutation path); `snapshot_readonly` is the side-effect
/// free variant.
pub fn list_events(workdir: &Path) -> Vec<Value> {
    let root = root(workdir);
    let mut events = Vec::new();
    if let Ok(entries) = fs::read_dir(&root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
                continue;
            }
            let Some(event) = read(&path) else {
                continue;
            };
            if event.get("schema").and_then(Value::as_str) != Some(SCHEMA) {
                continue;
            }
            if !ALLOWED_STATUSES
                .contains(&event.get("status").and_then(Value::as_str).unwrap_or(""))
            {
                continue;
            }
            let mut event = event;
            if event.get("delivery_state").is_none() {
                let status = event.get("status").and_then(Value::as_str).unwrap_or("");
                event["delivery_state"] = json!(if status == "acknowledged" {
                    "acknowledged"
                } else {
                    status
                });
            }
            if event.get("review_decision").is_none() {
                event["review_decision"] = event
                    .get("decision")
                    .cloned()
                    .unwrap_or_else(|| json!("pending"));
            }
            events.push(event);
        }
    }
    events.sort_by(|left, right| {
        let left_key = (
            left.get("created_at")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            left.get("event_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        );
        let right_key = (
            right
                .get("created_at")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            right
                .get("event_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        );
        left_key.cmp(&right_key)
    });
    events
}

/// Read every valid event without creating anything (a review session that
/// has never received a write reads as empty).
pub fn list_events_readonly(workdir: &Path) -> Vec<Value> {
    let inbox = workdir.join(QUEUE_DIR);
    if !inbox.is_dir() {
        return Vec::new();
    }
    let mut events = Vec::new();
    if let Ok(entries) = fs::read_dir(&inbox) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
                continue;
            }
            let Some(event) = read(&path) else {
                continue;
            };
            if event.get("schema").and_then(Value::as_str) != Some(SCHEMA) {
                continue;
            }
            if !ALLOWED_STATUSES
                .contains(&event.get("status").and_then(Value::as_str).unwrap_or(""))
            {
                continue;
            }
            let mut event = event;
            if event.get("delivery_state").is_none() {
                let status = event.get("status").and_then(Value::as_str).unwrap_or("");
                event["delivery_state"] = json!(if status == "acknowledged" {
                    "acknowledged"
                } else {
                    status
                });
            }
            if event.get("review_decision").is_none() {
                event["review_decision"] = event
                    .get("decision")
                    .cloned()
                    .unwrap_or_else(|| json!("pending"));
            }
            events.push(event);
        }
    }
    events.sort_by(|left, right| {
        let left_key = (
            left.get("created_at")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            left.get("event_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        );
        let right_key = (
            right
                .get("created_at")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            right
                .get("event_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        );
        left_key.cmp(&right_key)
    });
    events
}

/// Upsert one event: an existing draft with the same client_id is replaced
/// (same event_id, original created_at); otherwise a fresh draft is written.
pub fn upsert_event(workdir: &Path, event: &Value) -> Result<Value, String> {
    let normalized = validate_event(event)?;
    let existing = list_events(workdir).into_iter().find(|item| {
        item.get("client_id").and_then(Value::as_str)
            == normalized.get("client_id").and_then(Value::as_str)
            && item.get("status").and_then(Value::as_str) == Some("draft")
    });
    let now = now();
    let event_id = existing
        .as_ref()
        .and_then(|item| {
            item.get("event_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .or_else(|| {
            event
                .get("event_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .unwrap_or_else(new_operation_id);
    let record = json!({
        "schema": SCHEMA,
        "event_id": event_id,
        "status": "draft",
        "delivery_state": "staged",
        "created_at": existing.as_ref().and_then(|item| item.get("created_at").and_then(Value::as_str)).unwrap_or(&now),
        "updated_at": now,
    });
    let mut record = record.as_object().cloned().unwrap_or_default();
    for (key, value) in normalized.as_object().unwrap_or(&serde_json::Map::new()) {
        record.insert(key.clone(), value.clone());
    }
    // The queue schema wins over a normalized event's own schema (a patch
    // carries `docx2typed-document-patch-1`, but the stored review event is
    // always `docx2typed-review-event-1` — mirror of the Python upsert,
    // which sets `schema: SCHEMA` after merging).
    record.insert("schema".to_string(), json!(SCHEMA));
    let record = Value::Object(record);
    atomic_write(&event_path(workdir, &event_id)?, &record)?;
    Ok(record)
}

/// Apply a partial update to one event (session coordinates etc.).
pub fn update_event(workdir: &Path, event_id: &str, updates: &Value) -> Result<Value, String> {
    let path = event_path(workdir, event_id)?;
    let event = read(&path).ok_or_else(|| "event not found".to_string())?;
    if event.get("schema").and_then(Value::as_str) != Some(SCHEMA) {
        return Err("event not found".to_string());
    }
    let mut next = event.clone();
    for (key, value) in updates.as_object().unwrap_or(&serde_json::Map::new()) {
        next[key] = value.clone();
    }
    next["event_id"] = json!(event_id);
    next["schema"] = json!(SCHEMA);
    next["updated_at"] = json!(now());
    if !ALLOWED_STATUSES.contains(&next.get("status").and_then(Value::as_str).unwrap_or("")) {
        return Err("invalid event status".to_string());
    }
    if !ALLOWED_DELIVERY_STATES.contains(
        &next
            .get("delivery_state")
            .and_then(Value::as_str)
            .unwrap_or(""),
    ) {
        return Err("invalid delivery state".to_string());
    }
    if !ALLOWED_REVIEW_DECISIONS.contains(
        &next
            .get("review_decision")
            .and_then(Value::as_str)
            .unwrap_or(""),
    ) {
        return Err("invalid review decision".to_string());
    }
    atomic_write(&path, &next)?;
    Ok(next)
}

/// Move every draft into one queued batch (the agent wake call): status
/// queued, delivery_state queued, one shared batch_id.
pub fn dispatch(workdir: &Path) -> Result<Vec<Value>, String> {
    let batch_id = format!("batch-{}", &new_operation_id()[..12]);
    let mut queued = Vec::new();
    for event in list_events(workdir) {
        if event.get("status").and_then(Value::as_str) != Some("draft") {
            continue;
        }
        let event_id = event.get("event_id").and_then(Value::as_str).unwrap_or("");
        let queued_at = now();
        let next = update_event(
            workdir,
            event_id,
            &json!({
                "status": "queued",
                "delivery_state": "queued",
                "batch_id": batch_id,
                "queued_at": queued_at,
                "updated_at": queued_at,
            }),
        )?;
        queued.push(next);
    }
    Ok(queued)
}

/// Acknowledge queued events; already-acknowledged ones are returned
/// unchanged; other statuses are skipped.
pub fn acknowledge(workdir: &Path, event_ids: &[String]) -> Result<Vec<Value>, String> {
    let mut acknowledged = Vec::new();
    for event in list_events(workdir) {
        let Some(event_id) = event.get("event_id").and_then(Value::as_str) else {
            continue;
        };
        if !event_ids.iter().any(|wanted| wanted == event_id) {
            continue;
        }
        if event.get("status").and_then(Value::as_str) == Some("acknowledged") {
            acknowledged.push(event);
            continue;
        }
        if event.get("status").and_then(Value::as_str) != Some("queued") {
            continue;
        }
        let acked_at = now();
        let next = update_event(
            workdir,
            event_id,
            &json!({
                "status": "acknowledged",
                "delivery_state": "acknowledged",
                "acknowledged_at": acked_at,
                "updated_at": acked_at,
            }),
        )?;
        acknowledged.push(next);
    }
    Ok(acknowledged)
}

fn snapshot_dict(events: &[Value]) -> Value {
    let mut review_counts = serde_json::Map::new();
    for decision in ALLOWED_REVIEW_DECISIONS {
        review_counts.insert(
            decision.to_string(),
            json!(events
                .iter()
                .filter(
                    |event| event.get("review_decision").and_then(Value::as_str) == Some(decision)
                )
                .count()),
        );
    }
    json!({
        "schema": SCHEMA,
        "events": events,
        "counts": {
            "draft": events.iter().filter(|event| event.get("status").and_then(Value::as_str) == Some("draft")).count(),
            "queued": events.iter().filter(|event| event.get("status").and_then(Value::as_str) == Some("queued")).count(),
            "acknowledged": events.iter().filter(|event| event.get("status").and_then(Value::as_str) == Some("acknowledged")).count(),
        },
        "review_counts": Value::Object(review_counts),
    })
}

/// Queue snapshot (mutation path: creates the inbox directory).
pub fn snapshot(workdir: &Path) -> Value {
    snapshot_dict(&list_events(workdir))
}

/// Queue snapshot without any side effect.
pub fn snapshot_readonly(workdir: &Path) -> Value {
    snapshot_dict(&list_events_readonly(workdir))
}
