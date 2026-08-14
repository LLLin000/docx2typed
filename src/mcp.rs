//! MCP stdio adapter (issue #55 slice): one persistent stdio session using
//! the same line-delimited JSON protocol as the qualification harness driver
//! (`scripts/qualify_adapters.py` `_MCP_DRIVER_LOOP`): each stdin line is
//! `{"tool": ..., "args": ...}`, each reply is `OK <json>` or `ERR <msg>`.
//! Tools: `engine_info` (returns the engine descriptor directly) and
//! `workdir_open` (negotiates Protocol major 1, then opens one validated
//! workdir, returning the common Result envelope as `structuredContent`).
//!
//! Hand-rolled blocking loop over serde_json — no tokio needed for a stdio
//! tracer (issue #36: tokio only enters adapters when transports need it).

use std::io::{self, BufRead, Write};

use docx2typed_app::Engine;
use docx2typed_protocol::{
    engine_descriptor, negotiate, Diagnostic, NegotiationError, ResultEnvelope,
};
use serde_json::Value;

struct McpSession {
    engine: Engine,
    workdir: Option<String>,
}

impl McpSession {
    fn new() -> Self {
        McpSession {
            engine: Engine::new(),
            workdir: None,
        }
    }

    fn engine_info(&self, build_commit: &str) -> Value {
        serde_json::to_value(engine_descriptor(build_commit)).expect("descriptor serializes")
    }

    fn workdir_open(&mut self, args: &Value, build_commit: &str) -> Value {
        // Negotiation first (mirroring the Python tool): any mismatch is a
        // failure envelope, never an open.
        let workdir = match args.get("workdir").and_then(Value::as_str) {
            Some(workdir) => workdir.to_string(),
            None => {
                return self.tool_result(
                    "workdir_open",
                    "failure",
                    Value::Object(Default::default()),
                    vec![Diagnostic::with_details(
                        "invalid-arguments",
                        "workdir_open requires a workdir path".to_string(),
                        None,
                        None,
                    )],
                    true,
                    build_commit,
                )
            }
        };
        let author = args
            .get("author")
            .and_then(Value::as_str)
            .map(str::to_string);
        let contract_ranges = args.get("contract_ranges");
        let supported_features = args
            .get("supported_features")
            .and_then(Value::as_array)
            .map(|list| {
                list.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            });
        let required_features =
            args.get("required_features")
                .and_then(Value::as_array)
                .map(|list| {
                    list.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect::<Vec<_>>()
                });
        if let Err(error) = negotiate(
            contract_ranges,
            supported_features.as_deref(),
            required_features.as_deref(),
        ) {
            let (code, message, details) = match error {
                NegotiationError::ContractIncompatible {
                    contract,
                    engine_range,
                    client_range,
                } => (
                    "contract-incompatible",
                    format!("no compatible {contract} contract version"),
                    serde_json::json!({
                        "contract": contract,
                        "engine_range": engine_range,
                        "client_range": client_range,
                    }),
                ),
                NegotiationError::RequiredFeatureUnsupported { missing_features } => (
                    "required-feature-unsupported",
                    "required features are unsupported".to_string(),
                    serde_json::json!({ "missing_features": missing_features }),
                ),
            };
            return self.tool_result(
                "workdir_open",
                "failure",
                Value::Object(Default::default()),
                vec![Diagnostic::with_details(
                    code,
                    message,
                    Some(details),
                    Some(vec!["upgrade the incompatible client or engine".to_string()]),
                )],
                true,
                build_commit,
            );
        }
        if self.workdir.is_some() {
            return self.tool_result(
                "workdir_open",
                "failure",
                Value::Object(Default::default()),
                vec![Diagnostic::new(
                    "workdir-already-open",
                    "this MCP connection already has an open workdir".to_string(),
                )],
                true,
                build_commit,
            );
        }
        match self
            .engine
            .open_workdir_session(std::path::Path::new(&workdir), author.as_deref())
        {
            Ok(session) => {
                self.workdir = Some(workdir);
                self.tool_result(
                    "workdir_open",
                    "success",
                    serde_json::json!({ "session": session }),
                    vec![],
                    false,
                    build_commit,
                )
            }
            Err(message) => {
                let code = domain_code_from_message(&message);
                self.tool_result(
                    "workdir_open",
                    "failure",
                    Value::Object(Default::default()),
                    vec![Diagnostic::new(code, message)],
                    true,
                    build_commit,
                )
            }
        }
    }

    fn tool_result(
        &self,
        operation: &str,
        outcome: &str,
        data: Value,
        diagnostics: Vec<Diagnostic>,
        is_error: bool,
        build_commit: &str,
    ) -> Value {
        let envelope =
            ResultEnvelope::new(operation, outcome, data, diagnostics, vec![], build_commit);
        serde_json::json!({
            "content": [{"type": "text", "text": format!("{operation}: {outcome}")}],
            "structuredContent": serde_json::to_value(&envelope).expect("envelope serializes"),
            "isError": is_error,
        })
    }
}

/// Stable diagnostic code from a domain failure message prefix (kebab-code
/// prefix when registered; `workdir-invalid` fallback) — mirroring Python's
/// `domain_code_from_message`.
fn domain_code_from_message(message: &str) -> &'static str {
    let candidate = message
        .split(':')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
        .replace(' ', "-");
    match candidate.as_str() {
        "file not found" | "source file not found" => "input-not-found",
        "workdir not found" => "workdir-not-found",
        _ => "workdir-invalid",
    }
}

pub fn run(build_commit: &str) -> i32 {
    let mut session = McpSession::new();
    let stdin = io::stdin();
    let mut stdout = io::stdout();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue; // EOF arrives as Err; blank lines are not requests
        }
        let request: Value = match serde_json::from_str(&line) {
            Ok(request) => request,
            Err(error) => {
                let _ = writeln!(stdout, "ERR {error}");
                continue;
            }
        };
        let tool = request.get("tool").and_then(Value::as_str).unwrap_or("");
        let args = request.get("args").cloned().unwrap_or(Value::Null);
        let reply = match tool {
            "engine_info" => {
                let mut json = serde_json::to_string(&session.engine_info(build_commit))
                    .expect("descriptor serializes");
                json.insert_str(0, "OK ");
                json
            }
            "workdir_open" => {
                let mut json = serde_json::to_string(&session.workdir_open(&args, build_commit))
                    .expect("tool result serializes");
                json.insert_str(0, "OK ");
                json
            }
            other => format!("ERR unknown tool: {other}"),
        };
        if writeln!(stdout, "{reply}").is_err() {
            break;
        }
        let _ = stdout.flush();
    }
    0
}
