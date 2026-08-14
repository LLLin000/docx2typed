//! Versioned wire DTOs: `docx2typed-diagnostic-1` and `docx2typed-result-1`,
//! mirroring `scripts/protocol.py` (`diagnostic`/`domain_diagnostic`/
//! `result_envelope`).

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::descriptor::{engine_descriptor, EngineDescriptor};
use super::evidence::RunEvidence;
use crate::{DIAGNOSTIC_SCHEMA, RESULT_SCHEMA};

/// Frozen registry entry for one diagnostic code (values copied from
/// `scripts/protocol_schema_bundle.json` `diagnostics` for the codes this
/// slice emits).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DiagnosticSpec {
    pub severity: &'static str,
    pub category: &'static str,
    pub retriable: bool,
}

pub const DEFAULT_SPEC: DiagnosticSpec = DiagnosticSpec {
    severity: "error",
    category: "domain",
    retriable: false,
};

/// Registered spec for a code; unknown codes fall back to the stable domain
/// default (mirroring Python `domain_diagnostic`), so a failure envelope
/// never depends on registry membership.
pub fn diagnostic_spec(code: &str) -> DiagnosticSpec {
    match code {
        "input-not-found" => DiagnosticSpec {
            severity: "error",
            category: "input",
            retriable: false,
        },
        "workdir-not-found" => DiagnosticSpec {
            severity: "error",
            category: "workdir",
            retriable: false,
        },
        "workdir-invalid" => DiagnosticSpec {
            severity: "error",
            category: "workdir",
            retriable: false,
        },
        "workdir-unreadable" => DiagnosticSpec {
            severity: "error",
            category: "workdir",
            retriable: true,
        },
        "workdir-already-open" => DiagnosticSpec {
            severity: "error",
            category: "workdir",
            retriable: false,
        },
        "contract-incompatible" => DiagnosticSpec {
            severity: "error",
            category: "contract",
            retriable: false,
        },
        "required-feature-unsupported" => DiagnosticSpec {
            severity: "error",
            category: "contract",
            retriable: false,
        },
        "evidence-publish-failed" => DiagnosticSpec {
            severity: "error",
            category: "evidence",
            retriable: true,
        },
        "resource-limit-exceeded" => DiagnosticSpec {
            severity: "error",
            category: "input",
            retriable: false,
        },
        "invalid-arguments" => DiagnosticSpec {
            severity: "error",
            category: "invocation",
            retriable: false,
        },
        _ => DEFAULT_SPEC,
    }
}

/// `docx2typed-diagnostic-1`.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Diagnostic {
    pub schema: &'static str,
    pub code: String,
    pub severity: &'static str,
    pub category: &'static str,
    pub retriable: bool,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next_actions: Option<Vec<String>>,
}

impl Diagnostic {
    pub fn new(code: &str, message: String) -> Self {
        Self::with_details(code, message, None, None)
    }

    pub fn with_details(
        code: &str,
        message: String,
        details: Option<Value>,
        next_actions: Option<Vec<String>>,
    ) -> Self {
        let spec = diagnostic_spec(code);
        Diagnostic {
            schema: DIAGNOSTIC_SCHEMA,
            code: code.to_string(),
            severity: spec.severity,
            category: spec.category,
            retriable: spec.retriable,
            message,
            details,
            next_actions,
        }
    }
}

/// `docx2typed-result-1`.
#[derive(Clone, Debug, Serialize)]
pub struct ResultEnvelope {
    pub schema: &'static str,
    pub operation: String,
    pub outcome: String,
    pub data: Value,
    pub diagnostics: Vec<Diagnostic>,
    pub evidence: Vec<RunEvidence>,
    pub engine: EngineDescriptor,
}

impl ResultEnvelope {
    /// Mirror of `result_envelope(operation, outcome, ...)` in
    /// `scripts/protocol.py`; `engine` is the live descriptor with the
    /// build commit observed at call time.
    pub fn new(
        operation: &str,
        outcome: &str,
        data: Value,
        diagnostics: Vec<Diagnostic>,
        evidence: Vec<RunEvidence>,
        build_commit: &str,
    ) -> Self {
        ResultEnvelope {
            schema: RESULT_SCHEMA,
            operation: operation.to_string(),
            outcome: outcome.to_string(),
            data,
            diagnostics,
            evidence,
            engine: engine_descriptor(build_commit),
        }
    }
}
