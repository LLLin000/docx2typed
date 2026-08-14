//! Run-evidence records (`docx2typed-run-evidence-1`), mirroring
//! `scripts/protocol.py` (`base_evidence_payload`/`run_evidence`).

use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::Value;

use crate::{EVIDENCE_SCHEMA, PACKAGE_VERSION};

/// Engine/contract identity shared by every run-evidence payload.
pub fn base_evidence_payload() -> Value {
    serde_json::json!({
        "engine": {"name": "docx2typed-rust", "version": PACKAGE_VERSION},
        "contracts": {
            "result": {"major": 1, "min_minor": 0, "max_minor": 0},
            "evidence": {"major": 1, "min_minor": 0, "max_minor": 0},
        },
    })
}

/// One canonical immutable run-evidence record. `payload` is the semantic
/// part (hashes, checks) and must never carry document bodies or absolute
/// paths; `payload_sha256` covers exactly the canonical semantic payload;
/// provenance (time, run identity) is excluded from semantic equivalence.
#[derive(Clone, Debug, Serialize)]
pub struct RunEvidence {
    pub schema: &'static str,
    pub operation: String,
    pub outcome: String,
    pub kind: String,
    pub operation_id: String,
    pub payload: Value,
    pub payload_sha256: String,
    pub provenance: Provenance,
}

#[derive(Clone, Debug, Serialize)]
pub struct Provenance {
    pub run_id: String,
    pub started_at: String,
    pub finished_at: String,
}

pub fn run_evidence(
    operation: &str,
    outcome: &str,
    kind: &str,
    operation_id: &str,
    payload: Value,
) -> RunEvidence {
    let started_at = now_iso();
    let canonical_payload = serde_json::from_str::<Value>(
        &serde_json::to_string(&payload).expect("payload serializes"),
    )
    .expect("canonical payload reparse");
    let payload_sha256 = crate::semantic_sha256(&canonical_payload);
    RunEvidence {
        schema: EVIDENCE_SCHEMA,
        operation: operation.to_string(),
        outcome: outcome.to_string(),
        kind: kind.to_string(),
        operation_id: operation_id.to_string(),
        payload: canonical_payload,
        payload_sha256,
        provenance: Provenance {
            run_id: crate::new_operation_id(),
            started_at,
            finished_at: now_iso(),
        },
    }
}

/// UTC ISO-8601 with seconds precision (`YYYY-MM-DDTHH:MM:SS+00:00`),
/// mirroring Python `datetime.now(timezone.utc).isoformat(timespec="seconds")`.
fn now_iso() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
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
