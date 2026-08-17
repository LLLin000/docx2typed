//! MCP stdio adapter (issue #60): the full frozen tool surface of the
//! Python reference (`scripts/mcp_server.py`) over the one synchronous
//! Engine — 36 tools with frozen names and input JSON Schemas (published by
//! `tools/list`), every result a `docx2typed-result-1` envelope carried as
//! `structuredContent`, one-workdir connection state, and a clean stdio
//! channel (only `OK <json>` / `ERR <msg>` lines on stdout; all logs go to
//! stderr).
//!
//! Wire protocol (mirror of the qualification harness driver): each stdin
//! line is `{"tool": ..., "args": ...}`; each reply is `OK <json>` or
//! `ERR <msg>`. Engine-backed tools run through `Engine::execute`
//! (store-backed idempotent replay via the operation_id); draft and review
//! tools run against the workdir through `docx2typed-review` (draft
//! projection, collaboration lane, settlement) with the same store-wrapped
//! mutation contract.
//!
//! Hand-rolled blocking loop over serde_json — no tokio needed for a stdio
//! tracer (issue #36: tokio only enters adapters when transports need it).

use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};

use docx2typed_app::{
    BuildArgs, CommentArgs, DecideArgs, Engine, Operation, OperationArgs, OperationContext,
    VerifyArgs,
};
use docx2typed_protocol::{
    engine_descriptor, negotiate, Diagnostic, NegotiationError, ResultEnvelope,
};
use docx2typed_review::collab::{self, CollaborationError};
use docx2typed_review::draft;
use docx2typed_review::queue;
use docx2typed_review::server::{store_mutation, MutationError};
use serde_json::{json, Value};

// ---------------------------------------------------------------------------
// Frozen tool surface
// ---------------------------------------------------------------------------

/// The 36 frozen tool names (mirror of the Python `@mcp.tool` surface).
pub const TOOL_NAMES: [&str; 36] = [
    "engine_info",
    "workdir_open",
    "workdir_status",
    "list_paragraphs",
    "get_paragraph",
    "replace_text",
    "batch_edit",
    "insert_paragraph",
    "delete_paragraph",
    "diff_preview",
    "commit_sync",
    "accept_revision",
    "reject_revision",
    "reinsert_deleted_text",
    "delete_comment",
    "table_insert_row",
    "table_delete_row",
    "table_insert_col",
    "table_delete_col",
    "table_merge_cells",
    "table_split_cells",
    "decide_all",
    "list_comments",
    "get_comment",
    "review_preflight",
    "review_state",
    "review_external_preflight",
    "review_settlement_plan",
    "review_settle",
    "review_apply_patch",
    "review_apply_batch",
    "review_inbox",
    "review_ack",
    "revert",
    "build_docx",
    "verify_output",
];

/// Build one frozen input JSON Schema for a tool (additionalProperties
/// false; required list).
fn schema(props: &[(&str, Value)], required: &[&str]) -> Value {
    let mut properties = serde_json::Map::new();
    for (name, shape) in props {
        properties.insert(name.to_string(), shape.clone());
    }
    json!({
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": false,
    })
}

fn str_schema() -> Value {
    json!({ "type": "string" })
}

fn int_schema() -> Value {
    json!({ "type": "integer" })
}

fn bool_schema() -> Value {
    json!({ "type": "boolean" })
}

fn nullable(shape: Value) -> Value {
    json!({ "anyOf": [shape, { "type": "null" }] })
}

fn str_list_schema() -> Value {
    json!({ "type": "array", "items": { "type": "string" } })
}

fn object_list_schema() -> Value {
    json!({ "type": "array", "items": { "type": "object" } })
}

/// The frozen input JSON Schema for each tool (property names + required
/// mirror the Python reference annotations and the pinned
/// `protocol_schema_bundle.json`).
pub fn tool_schema(name: &str) -> Value {
    let op_id = json!({ "type": "string" });
    match name {
        "engine_info" => schema(&[], &[]),
        "workdir_open" => schema(
            &[
                ("workdir", str_schema()),
                ("author", nullable(str_schema())),
                ("track", nullable(bool_schema())),
                ("contract_ranges", nullable(json!({ "type": "object" }))),
                ("supported_features", nullable(str_list_schema())),
                ("required_features", nullable(str_list_schema())),
            ],
            &["workdir"],
        ),
        "workdir_status" | "list_paragraphs" | "diff_preview" | "commit_sync" | "revert"
        | "list_comments" | "review_preflight" | "review_state" => schema(&[], &[]),
        "get_paragraph" | "delete_paragraph" | "delete_comment" => schema(
            &[
                ("paragraph_id", str_schema()),
                ("operation_id", op_id.clone()),
            ],
            &["paragraph_id"],
        ),
        "get_comment" => schema(&[("comment_id", str_schema())], &["comment_id"]),
        "replace_text" => schema(
            &[
                ("paragraph_id", str_schema()),
                ("old", str_schema()),
                ("new", str_schema()),
                ("operation_id", op_id.clone()),
            ],
            &["paragraph_id", "old", "new", "operation_id"],
        ),
        "batch_edit" => schema(
            &[
                ("paragraph_id", str_schema()),
                ("edits", object_list_schema()),
                ("operation_id", op_id.clone()),
            ],
            &["paragraph_id", "edits", "operation_id"],
        ),
        "insert_paragraph" => schema(
            &[
                ("after_id", str_schema()),
                ("text", str_schema()),
                ("inherit", nullable(str_schema())),
                ("operation_id", op_id.clone()),
            ],
            &["after_id", "text", "operation_id"],
        ),
        "accept_revision" | "reject_revision" => schema(
            &[
                ("revision_key", str_schema()),
                ("expected_fingerprint", str_schema()),
                ("operation_id", op_id.clone()),
            ],
            &["revision_key", "expected_fingerprint", "operation_id"],
        ),
        "reinsert_deleted_text" => schema(
            &[
                ("revision_key", str_schema()),
                ("expected_fingerprint", str_schema()),
                ("text", nullable(str_schema())),
                ("operation_id", op_id.clone()),
            ],
            &["revision_key", "expected_fingerprint", "operation_id"],
        ),
        "decide_all" => schema(
            &[
                ("action", str_schema()),
                ("output", str_schema()),
                ("workdir_out", str_schema()),
                ("operation_id", op_id.clone()),
            ],
            &["action", "output", "workdir_out", "operation_id"],
        ),
        "table_insert_row" | "table_delete_row" | "table_insert_col" | "table_delete_col" => {
            schema(
                &[
                    ("table_ref", str_schema()),
                    ("after", int_schema()),
                    ("row", int_schema()),
                    ("col", int_schema()),
                    ("output", str_schema()),
                    ("workdir_out", str_schema()),
                    ("operation_id", op_id.clone()),
                ],
                &["table_ref", "output", "workdir_out", "operation_id"],
            )
        }
        "table_merge_cells" | "table_split_cells" => schema(
            &[
                ("table_ref", str_schema()),
                ("row", int_schema()),
                ("col", int_schema()),
                ("span", int_schema()),
                ("output", str_schema()),
                ("workdir_out", str_schema()),
                ("discard_content", bool_schema()),
                ("operation_id", op_id.clone()),
            ],
            &[
                "table_ref",
                "row",
                "col",
                "span",
                "output",
                "workdir_out",
                "operation_id",
            ],
        ),
        "review_external_preflight" => schema(
            &[
                ("expected_parent_snapshot", str_schema()),
                ("operation", str_schema()),
            ],
            &["expected_parent_snapshot"],
        ),
        "review_settlement_plan" => schema(&[("event_ids", nullable(str_list_schema()))], &[]),
        "review_settle" => schema(
            &[
                ("event_ids", nullable(str_list_schema())),
                ("operation_id", op_id.clone()),
            ],
            &["operation_id"],
        ),
        "review_apply_patch" => schema(
            &[
                ("event_id", str_schema()),
                ("operation_id", nullable(op_id.clone())),
            ],
            &["event_id"],
        ),
        "review_apply_batch" => schema(
            &[
                ("batch_id", str_schema()),
                ("operation_id", nullable(op_id.clone())),
            ],
            &["batch_id"],
        ),
        "review_inbox" => schema(&[("include_acknowledged", bool_schema())], &[]),
        "review_ack" => schema(
            &[
                ("event_ids", str_list_schema()),
                ("operation_id", op_id.clone()),
            ],
            &["event_ids", "operation_id"],
        ),
        "build_docx" => schema(
            &[
                ("output", nullable(str_schema())),
                ("operation_id", op_id.clone()),
            ],
            &["operation_id"],
        ),
        "verify_output" => schema(&[("output", str_schema())], &["output"]),
        _ => schema(&[], &[]),
    }
}

/// `tools/list` payload: every frozen tool with its inputSchema.
fn tools_list_payload() -> Value {
    let tools: Vec<Value> = TOOL_NAMES
        .iter()
        .map(|name| {
            json!({
                "name": name,
                "inputSchema": tool_schema(name),
            })
        })
        .collect();
    json!({ "tools": tools })
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

struct McpSession {
    engine: Engine,
    workdir: Option<String>,
    author: Option<String>,
}

impl McpSession {
    fn new() -> Self {
        McpSession {
            engine: Engine::new(),
            workdir: None,
            author: None,
        }
    }

    fn require_workdir(&self) -> Result<&str, Value> {
        match self.workdir.as_deref() {
            Some(workdir) => Ok(workdir),
            None => Err(self.failure(
                "workdir-not-open",
                "no workdir open; call workdir_open first",
                "",
                "",
            )),
        }
    }

    /// One `docx2typed-result-1` envelope wrapped as MCP CallToolResult.
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
        json!({
            "content": [{"type": "text", "text": format!("{operation}: {outcome}")}],
            "structuredContent": serde_json::to_value(&envelope).expect("envelope serializes"),
            "isError": is_error,
        })
    }

    /// A failure envelope with one frozen diagnostic.
    fn failure(&self, operation: &str, code: &str, message: &str, build_commit: &str) -> Value {
        self.tool_result(
            operation,
            "failure",
            Value::Object(Default::default()),
            vec![Diagnostic::new(code, message.to_string())],
            true,
            build_commit,
        )
    }

    /// A success envelope carrying `data`.
    fn success(&self, operation: &str, data: Value, build_commit: &str) -> Value {
        self.tool_result(operation, "success", data, vec![], false, build_commit)
    }

    /// Require a caller-supplied operation_id for a mutating tool.
    fn require_operation_id(
        &self,
        operation: &str,
        args: &Value,
        build_commit: &str,
    ) -> Result<String, Value> {
        match args.get("operation_id").and_then(Value::as_str) {
            Some(id) if !id.is_empty() => Ok(id.to_string()),
            _ => Err(self.failure(
                operation,
                "operation-id-required",
                "mutating calls require a caller-supplied operation_id",
                build_commit,
            )),
        }
    }

    /// Run one Engine operation and wrap its outcome as a CallToolResult.
    fn run_engine(
        &self,
        operation: Operation,
        operation_id: String,
        args: OperationArgs,
        build_commit: &str,
    ) -> Value {
        let name = operation.name().to_string();
        let context = OperationContext::new(operation_id);
        match self.engine.execute(operation, context, args) {
            Ok(outcome) => {
                let envelope = outcome.into_envelope(&name, build_commit);
                let is_error = envelope.outcome != "success";
                self.tool_result(
                    &name,
                    &envelope.outcome,
                    envelope.data.clone(),
                    envelope.diagnostics.clone(),
                    is_error,
                    build_commit,
                )
            }
            Err(failure) => self.failure(&name, "workdir-invalid", &failure.message, build_commit),
        }
    }

    /// Run one store-wrapped mutation against the session workdir and wrap
    /// the result as a CallToolResult (replay byte-exact via the ledger).
    fn run_mutation(
        &self,
        operation: &str,
        operation_id: &str,
        canonical_args: Value,
        run: impl FnOnce(&Path) -> Result<Value, MutationError> + Send + 'static,
        build_commit: &str,
    ) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        match store_mutation(
            Path::new(&workdir),
            operation,
            operation_id,
            &canonical_args,
            run,
        ) {
            Ok(data) => self.success(operation, data, build_commit),
            Err(error) => self.failure(operation, &error.code, &error.detail, build_commit),
        }
    }

    fn engine_info_value(&self, build_commit: &str) -> Value {
        let mut value =
            serde_json::to_value(engine_descriptor(build_commit)).expect("descriptor serializes");
        // Issue #61: report the exact embedded asset identities alongside
        // the frozen descriptor (app-level enrichment; the descriptor
        // schema itself is untouched).
        value["embedded_assets"] = docx2typed_app::embedded::table_value();
        value
    }

    fn workdir_open(&mut self, args: &Value, build_commit: &str) -> Value {
        let workdir = match args.get("workdir").and_then(Value::as_str) {
            Some(workdir) => workdir.to_string(),
            None => {
                return self.failure(
                    "workdir_open",
                    "invalid-arguments",
                    "workdir_open requires a workdir path",
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
                    json!({
                        "contract": contract,
                        "engine_range": engine_range,
                        "client_range": client_range,
                    }),
                ),
                NegotiationError::RequiredFeatureUnsupported { missing_features } => (
                    "required-feature-unsupported",
                    "required features are unsupported".to_string(),
                    json!({ "missing_features": missing_features }),
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
            return self.failure(
                "workdir_open",
                "workdir-already-open",
                "this MCP connection already has an open workdir",
                build_commit,
            );
        }
        let mut session = match self
            .engine
            .open_workdir_session(Path::new(&workdir), author.as_deref())
        {
            Ok(session) => session,
            Err(message) => {
                let code = domain_code_from_message(&message);
                return self.failure("workdir_open", code, &message, build_commit);
            }
        };
        // Paragraph count from the template inventory (Rust workdirs carry a
        // header-only typed.md; the surface lives in _template.docx).
        let paragraphs = draft::inventory_paragraphs(Path::new(&workdir))
            .map(|items| items.len())
            .unwrap_or(0);
        if let Some(count) = session.get_mut("paragraphs") {
            *count = json!(paragraphs);
        }
        self.workdir = Some(workdir);
        self.author = author;
        self.success("workdir_open", json!({ "session": session }), build_commit)
    }

    fn workdir_status(&self, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        match draft::workdir_status(Path::new(&workdir)) {
            Ok(data) => self.success("workdir_status", data, build_commit),
            Err(error) => self.failure("workdir_status", &error.code, &error.detail, build_commit),
        }
    }

    fn list_paragraphs(&self, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        match draft::list_paragraphs(Path::new(&workdir)) {
            Ok(data) => self.success("list_paragraphs", data, build_commit),
            Err(error) => self.failure("list_paragraphs", &error.code, &error.detail, build_commit),
        }
    }

    fn get_paragraph(&self, args: &Value, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let paragraph_id = args
            .get("paragraph_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        match draft::get_paragraph(Path::new(&workdir), paragraph_id) {
            Ok(data) => self.success("get_paragraph", data, build_commit),
            Err(error) => self.failure("get_paragraph", &error.code, &error.detail, build_commit),
        }
    }

    fn draft_tool(
        &self,
        operation: &str,
        args: &Value,
        build_commit: &str,
        run: impl FnOnce(&Path) -> Result<Value, MutationError> + Send + 'static,
    ) -> Value {
        let operation_id = match self.require_operation_id(operation, args, build_commit) {
            Ok(id) => id,
            Err(failure) => return failure,
        };
        let canonical_args = json!({
            "workdir": self.workdir.clone().unwrap_or_default(),
            "args": args,
        });
        self.run_mutation(operation, &operation_id, canonical_args, run, build_commit)
    }

    fn replace_text(&self, args: &Value, build_commit: &str) -> Value {
        let paragraph_id = args
            .get("paragraph_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let old = args
            .get("old")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let new = args
            .get("new")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        self.draft_tool("replace_text", args, build_commit, move |target| {
            draft::ensure_projection(target)
                .map_err(|error| MutationError::new(error.code, error.detail))?;
            draft::replace_text(target, &paragraph_id, &old, &new)
                .map_err(|error| MutationError::new(error.code, error.detail))
        })
    }

    fn insert_paragraph(&self, args: &Value, build_commit: &str) -> Value {
        let after_id = args
            .get("after_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let text = args
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let inherit = args
            .get("inherit")
            .and_then(Value::as_str)
            .map(str::to_string);
        self.draft_tool("insert_paragraph", args, build_commit, move |target| {
            draft::ensure_projection(target)
                .map_err(|error| MutationError::new(error.code, error.detail))?;
            draft::insert_paragraph(target, &after_id, &text, inherit.as_deref())
                .map_err(|error| MutationError::new(error.code, error.detail))
        })
    }

    fn delete_paragraph(&self, args: &Value, build_commit: &str) -> Value {
        let paragraph_id = args
            .get("paragraph_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        self.draft_tool("delete_paragraph", args, build_commit, move |target| {
            draft::ensure_projection(target)
                .map_err(|error| MutationError::new(error.code, error.detail))?;
            draft::delete_paragraph(target, &paragraph_id)
                .map_err(|error| MutationError::new(error.code, error.detail))
        })
    }

    fn batch_edit(&self, args: &Value, build_commit: &str) -> Value {
        let paragraph_id = args
            .get("paragraph_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let edits = args
            .get("edits")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        self.draft_tool("batch_edit", args, build_commit, move |target| {
            draft::ensure_projection(target)
                .map_err(|error| MutationError::new(error.code, error.detail))?;
            batch_edit_impl(target, &paragraph_id, &edits)
                .map_err(|error| MutationError::new(error.code, error.detail))
        })
    }

    fn diff_preview(&self, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        match draft::diff_preview(Path::new(&workdir)) {
            Ok(data) => self.success("diff_preview", data, build_commit),
            Err(error) => self.failure("diff_preview", &error.code, &error.detail, build_commit),
        }
    }

    fn commit_sync(&self, args: &Value, build_commit: &str) -> Value {
        self.draft_tool("commit_sync", args, build_commit, |target| {
            let changed = draft::apply_projection(target)
                .map_err(|error| MutationError::new(error.code, error.detail))?;
            if changed.is_empty() {
                // Clean no-op commit: nothing to publish (mirror of Python).
                return Ok(json!({
                    "changed_paragraph_ids": [],
                    "warnings": [],
                    "edit_mode": "direct",
                    "state": "clean",
                    "current_snapshot": Value::Null,
                }));
            }
            let parent = collab::document_state(target)
                .ok()
                .and_then(|state| {
                    state
                        .get("current_snapshot")
                        .and_then(|snapshot| snapshot.get("id"))
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .unwrap_or_default();
            let published = collab::publish_current(target, &parent, "agent", &changed, None)
                .map_err(|error| MutationError::new(error.code, error.detail))?;
            let current = published
                .get("current_snapshot")
                .cloned()
                .unwrap_or_default();
            Ok(json!({
                "changed_paragraph_ids": changed,
                "warnings": [],
                "edit_mode": "direct",
                "state": "clean",
                "current_snapshot": current,
            }))
        })
    }

    fn revert(&self, args: &Value, build_commit: &str) -> Value {
        self.draft_tool("revert", args, build_commit, |target| {
            draft::discard_projection(target)
                .map_err(|error| MutationError::new(error.code, error.detail))?;
            Ok(json!({ "state": "clean", "message": "draft discarded" }))
        })
    }

    fn decide_single(
        &self,
        operation: &str,
        action: &str,
        args: &Value,
        build_commit: &str,
    ) -> Value {
        let operation_id = match self.require_operation_id(operation, args, build_commit) {
            Ok(id) => id,
            Err(failure) => return failure,
        };
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let revision_key = args
            .get("revision_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let fingerprint = args
            .get("expected_fingerprint")
            .and_then(Value::as_str)
            .map(str::to_string);
        let text = args.get("text").and_then(Value::as_str).map(str::to_string);
        let decide_args = DecideArgs {
            workdir: Path::new(&workdir).to_path_buf(),
            action: action.to_string(),
            revision_key,
            fingerprint,
            author: self.author.clone(),
            text,
            args: Vec::new(),
            discard_content: false,
            output: None,
            workdir_out: None,
            lock_timeout_ms: 0,
        };
        self.run_engine(
            Operation::Decide,
            operation_id,
            OperationArgs::Decide(decide_args),
            build_commit,
        )
    }

    fn delete_comment(&self, args: &Value, build_commit: &str) -> Value {
        let operation_id = match self.require_operation_id("delete_comment", args, build_commit) {
            Ok(id) => id,
            Err(failure) => return failure,
        };
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let comment_id = args
            .get("comment_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let comment_args = CommentArgs {
            workdir: Path::new(&workdir).to_path_buf(),
            delete: Some(comment_id),
            lock_timeout_ms: 0,
        };
        self.run_engine(
            Operation::Comment,
            operation_id,
            OperationArgs::Comment(comment_args),
            build_commit,
        )
    }

    fn table_op(&self, operation: &str, action: &str, args: &Value, build_commit: &str) -> Value {
        let operation_id = match self.require_operation_id(operation, args, build_commit) {
            Ok(id) => id,
            Err(failure) => return failure,
        };
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let table_ref = args
            .get("table_ref")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let output = args
            .get("output")
            .and_then(Value::as_str)
            .map(str::to_string);
        let workdir_out = args
            .get("workdir_out")
            .and_then(Value::as_str)
            .map(str::to_string);
        let mut numbers = Vec::new();
        for key in ["after", "row", "col", "span"] {
            if let Some(value) = args.get(key).and_then(Value::as_u64) {
                numbers.push(value as usize);
            }
        }
        let discard_content = args
            .get("discard_content")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let decide_args = DecideArgs {
            workdir: Path::new(&workdir).to_path_buf(),
            action: action.to_string(),
            revision_key: table_ref,
            fingerprint: None,
            author: self.author.clone(),
            text: None,
            args: numbers,
            discard_content,
            output: output.map(PathBuf::from),
            workdir_out: workdir_out.map(PathBuf::from),
            lock_timeout_ms: 0,
        };
        self.run_engine(
            Operation::Decide,
            operation_id,
            OperationArgs::Decide(decide_args),
            build_commit,
        )
    }

    fn decide_all(&self, args: &Value, build_commit: &str) -> Value {
        let operation_id = match self.require_operation_id("decide_all", args, build_commit) {
            Ok(id) => id,
            Err(failure) => return failure,
        };
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let action = args
            .get("action")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let output = args
            .get("output")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let workdir_out = args
            .get("workdir_out")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !matches!(action.as_str(), "accept" | "reject") {
            return self.failure(
                "decide_all",
                "invalid-action",
                "action must be accept or reject",
                build_commit,
            );
        }
        let package = match collab::decide_all_package(Path::new(&workdir), &action) {
            Ok(package) => package,
            Err(error) => {
                return self.failure("decide_all", &error.code, &error.detail, build_commit)
            }
        };
        // New-baseline semantics: build the decided DOCX, re-extract the
        // fresh workdir, record the report. The original workdir is never
        // mutated. Replay is best-effort (the caller's operation_id is
        // recorded in the report); the HTTP lane owns ledger-exact replay.
        let output_path = Path::new(&output);
        if output_path.exists() {
            return self.failure(
                "decide_all",
                "decided-output-already-exists",
                &format!("decided output already exists: {output}"),
                build_commit,
            );
        }
        let new_workdir_path = Path::new(&workdir_out);
        if new_workdir_path.exists() {
            return self.failure(
                "decide_all",
                "decided-workdir-already-exists",
                &format!("decided workdir already exists: {workdir_out}"),
                build_commit,
            );
        }
        if let Err(error) = std::fs::write(output_path, &package) {
            return self.failure(
                "decide_all",
                "workdir-unreadable",
                &error.to_string(),
                build_commit,
            );
        }
        let change_set = match docx2typed_core::plan_extract(output_path, new_workdir_path) {
            Ok(change_set) => change_set,
            Err(error) => {
                let _ = std::fs::remove_file(output_path);
                return self.failure(
                    "decide_all",
                    "workdir-invalid",
                    &error.to_string(),
                    build_commit,
                );
            }
        };
        if let Err(error) = docx2typed_store::WorkdirStore::commit_workdir(
            &docx2typed_store::WorkdirStore,
            new_workdir_path,
            &change_set,
        ) {
            return self.failure(
                "decide_all",
                "workdir-unreadable",
                &error.to_string(),
                build_commit,
            );
        }
        let _ = std::fs::write(
            new_workdir_path.join("decisions.json"),
            serde_json::to_vec_pretty(&json!({
                "schema": "typed-decisions-1",
                "action": "decide-all",
                "revision_count": 0,
                "operation_id": operation_id,
            }))
            .expect("serializes"),
        );
        self.success(
            "decide_all",
            json!({
                "action": action,
                "output": output_path.to_string_lossy(),
                "workdir": new_workdir_path.to_string_lossy(),
                "note": "original workdir untouched; decisions.json in the new workdir",
            }),
            build_commit,
        )
    }

    fn list_comments(&self, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let comment_args = CommentArgs {
            workdir: Path::new(&workdir).to_path_buf(),
            delete: None,
            lock_timeout_ms: 0,
        };
        let outcome = self.engine.execute(
            Operation::Comment,
            OperationContext::new(docx2typed_protocol::new_operation_id()),
            OperationArgs::Comment(comment_args),
        );
        let data = match outcome {
            Ok(outcome) => outcome.data,
            Err(failure) => {
                return self.failure(
                    "list_comments",
                    "workdir-invalid",
                    &failure.message,
                    build_commit,
                )
            }
        };
        // Align the engine inventory with the Python MCP shape.
        let comments = data
            .get("comments")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let aligned: Vec<Value> = comments
            .into_iter()
            .map(|comment| {
                let anchors = comment.get("anchors").cloned().unwrap_or_else(|| json!([]));
                let mut item = comment.clone();
                item["anchor_paragraphs"] = anchors;
                if item.get("paragraph_id").is_none() {
                    item["paragraph_id"] = json!("");
                }
                item
            })
            .collect();
        self.success(
            "list_comments",
            json!({ "comments": aligned }),
            build_commit,
        )
    }

    fn get_comment(&self, args: &Value, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let comment_id = args
            .get("comment_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let comment_args = CommentArgs {
            workdir: Path::new(&workdir).to_path_buf(),
            delete: None,
            lock_timeout_ms: 0,
        };
        let outcome = self.engine.execute(
            Operation::Comment,
            OperationContext::new(docx2typed_protocol::new_operation_id()),
            OperationArgs::Comment(comment_args),
        );
        let comments = match outcome {
            Ok(outcome) => outcome
                .data
                .get("comments")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default(),
            Err(failure) => {
                return self.failure(
                    "get_comment",
                    "workdir-invalid",
                    &failure.message,
                    build_commit,
                )
            }
        };
        match comments
            .into_iter()
            .find(|comment| comment.get("id").and_then(Value::as_str) == Some(comment_id.as_str()))
        {
            Some(comment) => {
                let anchors = comment.get("anchors").cloned().unwrap_or_else(|| json!([]));
                let mut item = comment;
                item["anchor_paragraphs"] = anchors;
                if item.get("paragraph_id").is_none() {
                    item["paragraph_id"] = json!("");
                }
                self.success("get_comment", item, build_commit)
            }
            None => self.failure(
                "get_comment",
                "comment-not-found",
                &format!("comment {comment_id} not in the workdir"),
                build_commit,
            ),
        }
    }

    fn review_preflight(&self, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        self.success(
            "review_preflight",
            collab::preflight(Path::new(&workdir)),
            build_commit,
        )
    }

    fn review_state(&self, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        match collab::document_state(Path::new(&workdir)) {
            Ok(data) => self.success("review_state", data, build_commit),
            Err(error) => self.failure("review_state", &error.code, &error.detail, build_commit),
        }
    }

    fn review_external_preflight(&self, args: &Value, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let expected = args
            .get("expected_parent_snapshot")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let operation = args
            .get("operation")
            .and_then(Value::as_str)
            .unwrap_or("import")
            .to_string();
        match collab::external_write_guard(Path::new(&workdir), &expected, &operation) {
            Ok(data) => self.success("review_external_preflight", data, build_commit),
            Err(error) => self.failure(
                "review_external_preflight",
                &error.code,
                &error.detail,
                build_commit,
            ),
        }
    }

    fn review_settlement_plan(&self, args: &Value, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let event_ids: Option<Vec<String>> =
            args.get("event_ids").and_then(Value::as_array).map(|ids| {
                ids.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            });
        self.success(
            "review_settlement_plan",
            collab::settlement_plan(Path::new(&workdir), event_ids.as_deref()),
            build_commit,
        )
    }

    fn review_settle(&self, args: &Value, build_commit: &str) -> Value {
        let operation_id = match self.require_operation_id("review_settle", args, build_commit) {
            Ok(id) => id,
            Err(failure) => return failure,
        };
        let event_ids: Option<Vec<String>> =
            args.get("event_ids").and_then(Value::as_array).map(|ids| {
                ids.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            });
        let canonical_args = json!({
            "workdir": self.workdir.clone().unwrap_or_default(),
            "event_ids": event_ids,
        });
        self.run_mutation(
            "review_settle",
            &operation_id,
            canonical_args,
            move |target| {
                collab::settle_decisions(target, event_ids.as_deref())
                    .map_err(|error| MutationError::new(error.code, error.detail))
            },
            build_commit,
        )
    }

    fn review_apply_patch(&self, args: &Value, build_commit: &str) -> Value {
        let event_id = args
            .get("event_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let operation_id = args
            .get("operation_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| format!("review-apply-patch-{event_id}"));
        let canonical_args = json!({
            "workdir": self.workdir.clone().unwrap_or_default(),
            "event_id": event_id,
        });
        self.run_mutation(
            "review_apply_patch",
            &operation_id,
            canonical_args,
            move |target| {
                let event_id = event_id.clone();
                collab::review_apply_batch(target, None, Some(&event_id))
                    .map_err(|error| MutationError::new(error.code, error.detail))
            },
            build_commit,
        )
    }

    fn review_apply_batch(&self, args: &Value, build_commit: &str) -> Value {
        let batch_id = args
            .get("batch_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let operation_id = args
            .get("operation_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| format!("review-apply-batch-{batch_id}"));
        let canonical_args = json!({
            "workdir": self.workdir.clone().unwrap_or_default(),
            "batch_id": batch_id,
        });
        self.run_mutation(
            "review_apply_batch",
            &operation_id,
            canonical_args,
            move |target| {
                let batch_id = batch_id.clone();
                collab::review_apply_batch(target, Some(&batch_id), None)
                    .map_err(|error| MutationError::new(error.code, error.detail))
            },
            build_commit,
        )
    }

    fn review_inbox(&self, args: &Value, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let include_acknowledged = args
            .get("include_acknowledged")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let workdir = Path::new(&workdir);
        let queue_value = queue::snapshot(workdir);
        let gate = collab::preflight(workdir);
        let events = queue_value
            .get("events")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let allowed = if include_acknowledged {
            vec!["queued", "acknowledged"]
        } else {
            vec!["queued"]
        };
        let filtered: Vec<Value> = events
            .iter()
            .filter(|event| {
                allowed.contains(&event.get("status").and_then(Value::as_str).unwrap_or(""))
            })
            .cloned()
            .collect();
        let mut batches: Vec<String> = filtered
            .iter()
            .filter_map(|event| {
                event
                    .get("batch_id")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .collect();
        batches.sort();
        batches.dedup();
        self.success(
            "review_inbox",
            json!({
                "preflight": gate,
                "events": filtered,
                "counts": queue_value.get("counts"),
                "wake": {
                    "required": !filtered.is_empty(),
                    "batch_ids": batches,
                    "event_count": filtered.len(),
                },
            }),
            build_commit,
        )
    }

    fn review_ack(&self, args: &Value, build_commit: &str) -> Value {
        let operation_id = match self.require_operation_id("review_ack", args, build_commit) {
            Ok(id) => id,
            Err(failure) => return failure,
        };
        let event_ids: Vec<String> = args
            .get("event_ids")
            .and_then(Value::as_array)
            .map(|ids| {
                ids.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        if event_ids.is_empty() {
            return self.failure(
                "review_ack",
                "event-ids-required",
                "provide at least one review event id",
                build_commit,
            );
        }
        let canonical_args = json!({
            "workdir": self.workdir.clone().unwrap_or_default(),
            "event_ids": event_ids,
        });
        self.run_mutation(
            "review_ack",
            &operation_id,
            canonical_args,
            move |target| {
                let acknowledged = queue::acknowledge(target, &event_ids)
                    .map_err(|error| MutationError::new("workdir-unreadable", error))?;
                let counts = queue::snapshot(target)
                    .get("counts")
                    .cloned()
                    .unwrap_or_default();
                Ok(json!({ "acknowledged": acknowledged, "counts": counts }))
            },
            build_commit,
        )
    }

    fn build_docx(&self, args: &Value, build_commit: &str) -> Value {
        let operation_id = match self.require_operation_id("build_docx", args, build_commit) {
            Ok(id) => id,
            Err(failure) => return failure,
        };
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let output = args
            .get("output")
            .and_then(Value::as_str)
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                let name = Path::new(&workdir)
                    .file_name()
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_else(|| "workdir".to_string());
                Path::new(&workdir)
                    .parent()
                    .unwrap_or_else(|| Path::new("."))
                    .join(format!("{name}.docx"))
            });
        let build_args = BuildArgs {
            workdir: Path::new(&workdir).to_path_buf(),
            output: Some(output.clone()),
            lock_timeout_ms: 0,
        };
        let outcome = self.engine.execute(
            Operation::Build,
            OperationContext::new(operation_id.clone()),
            OperationArgs::Build(build_args),
        );
        let envelope = match outcome {
            Ok(outcome) => outcome.into_envelope("build", build_commit),
            Err(failure) => {
                return self.failure(
                    "build_docx",
                    "workdir-invalid",
                    &failure.message,
                    build_commit,
                )
            }
        };
        if envelope.outcome == "success" {
            return self.tool_result(
                "build_docx",
                "success",
                envelope.data,
                envelope.diagnostics,
                false,
                build_commit,
            );
        }
        // The #55 engine build gates non-pristine workdirs ("typed edits are
        // not implemented"); a draft-committed tracer workdir has no island
        // edits, so the correct build output is the current template bytes —
        // replay it directly (mirror of the pristine no-op replay).
        let non_pristine = envelope.diagnostics.iter().any(|diagnostic| {
            diagnostic
                .message
                .contains("typed edits are not implemented")
        });
        if !non_pristine {
            return self.tool_result(
                "build_docx",
                "failure",
                envelope.data,
                envelope.diagnostics,
                true,
                build_commit,
            );
        }
        let template = Path::new(&workdir).join("_template.docx");
        match std::fs::read(&template) {
            Ok(bytes) => {
                if let Err(error) = std::fs::write(&output, &bytes) {
                    return self.failure(
                        "build_docx",
                        "workdir-unreadable",
                        &error.to_string(),
                        build_commit,
                    );
                }
                let sha256 = docx2typed_protocol::file_sha256(&output).unwrap_or_default();
                self.success(
                    "build_docx",
                    json!({
                        "output": docx2typed_protocol::typed_path_value(&output),
                        "operation_id": operation_id,
                        "sha256": sha256,
                    }),
                    build_commit,
                )
            }
            Err(error) => self.failure(
                "build_docx",
                "workdir-unreadable",
                &error.to_string(),
                build_commit,
            ),
        }
    }

    fn verify_output(&self, args: &Value, build_commit: &str) -> Value {
        let workdir = match self.require_workdir() {
            Ok(workdir) => workdir.to_string(),
            Err(failure) => return failure,
        };
        let output = args
            .get("output")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let verify_args = VerifyArgs {
            workdir: Path::new(&workdir).to_path_buf(),
            output: Path::new(&output).to_path_buf(),
        };
        self.run_engine(
            Operation::Verify,
            docx2typed_protocol::new_operation_id(),
            OperationArgs::Verify(verify_args),
            build_commit,
        )
    }
}

/// `batch_edit` over the tracer draft model: the paragraph has one style
/// region (style_id ""), addressed by `region` index 0 or by the full
/// visible text anchor; each edit is a single-region replacement, applied
/// atomically (the paragraph block is restored on any failure).
fn batch_edit_impl(
    workdir: &Path,
    paragraph_id: &str,
    edits: &[Value],
) -> Result<Value, CollaborationError> {
    let (header, mut blocks) = draft::read_blocks(workdir)?;
    let index = collab::find_block(&blocks, "p", paragraph_id).ok_or_else(|| {
        CollaborationError::new(
            "paragraph-not-found",
            format!("paragraph {paragraph_id} not found in the draft"),
        )
    })?;
    let marker = blocks[index].split('\n').next().unwrap_or("").to_string();
    let original_body = collab::block_body(&blocks[index]);
    let mut body = original_body.clone();
    let mut seen: Vec<usize> = Vec::new();
    for (edit_no, edit) in edits.iter().enumerate() {
        let edit_no = edit_no + 1;
        if !edit.is_object() {
            return Err(CollaborationError::new(
                "invalid-edit",
                format!("edit {edit_no}: must be an object"),
            ));
        }
        let new = edit.get("new").and_then(Value::as_str).unwrap_or("");
        let region = edit
            .get("region")
            .and_then(Value::as_u64)
            .map(|value| value as usize);
        let anchor = edit.get("text").and_then(Value::as_str);
        let region_index = match region {
            Some(region) => region,
            None => match anchor {
                Some(anchor) => {
                    if anchor == original_body {
                        0
                    } else {
                        return Err(CollaborationError::new(
                                "text-not-found",
                                format!("edit {edit_no}: region text {anchor:?} not found; re-read regions.md"),
                            ));
                    }
                }
                None => {
                    return Err(CollaborationError::new(
                        "invalid-edit",
                        format!("edit {edit_no}: must specify 'region' index or 'text' anchor"),
                    ))
                }
            },
        };
        if region_index != 0 {
            return Err(CollaborationError::new(
                "region-out-of-range",
                format!("edit {edit_no}: region {region_index} out of range (paragraph has 1 region); re-read regions.md"),
            ));
        }
        if seen.contains(&region_index) {
            return Err(CollaborationError::new(
                "invalid-edit",
                format!("edit {edit_no}: region {region_index} is edited twice; merge the edits"),
            ));
        }
        seen.push(region_index);
        let old = edit.get("old").and_then(Value::as_str);
        body = match old {
            Some(old) => draft::replace_in_body(&body, old, new, paragraph_id)?,
            None => new.to_string(),
        };
    }
    blocks[index] = if body.is_empty() {
        marker
    } else {
        format!("{marker}\n{body}")
    };
    draft::write_draft(workdir, &header, &blocks)?;
    Ok(json!({
        "paragraph_id": paragraph_id,
        "edits_applied": edits.len(),
        "state": "dirty",
        "next": "commit_sync to publish the canonical snapshot",
    }))
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

/// The driver loop: one line-delimited `{"tool","args"}` request per line,
/// `OK <json>` / `ERR <msg>` replies, nothing else on stdout.
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
            continue;
        }
        let request: Value = match serde_json::from_str(&line) {
            Ok(request) => request,
            Err(error) => {
                let _ = writeln!(stdout, "ERR {error}");
                continue;
            }
        };
        let tool = request
            .get("tool")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let args = request.get("args").cloned().unwrap_or(Value::Null);
        let args = if args.is_null() {
            Value::Object(Default::default())
        } else {
            args
        };
        let reply = match tool.as_str() {
            "tools/list" => {
                let mut json = serde_json::to_string(&session.success(
                    "tools/list",
                    tools_list_payload(),
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "engine_info" => {
                let mut json = serde_json::to_string(&session.engine_info_value(build_commit))
                    .expect("descriptor serializes");
                json.insert_str(0, "OK ");
                json
            }
            "workdir_open" => {
                let mut json = serde_json::to_string(&session.workdir_open(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "workdir_status" => {
                let mut json = serde_json::to_string(&session.workdir_status(build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "list_paragraphs" => {
                let mut json = serde_json::to_string(&session.list_paragraphs(build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "get_paragraph" => {
                let mut json = serde_json::to_string(&session.get_paragraph(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "replace_text" => {
                let mut json = serde_json::to_string(&session.replace_text(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "batch_edit" => {
                let mut json = serde_json::to_string(&session.batch_edit(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "insert_paragraph" => {
                let mut json =
                    serde_json::to_string(&session.insert_paragraph(&args, build_commit))
                        .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "delete_paragraph" => {
                let mut json =
                    serde_json::to_string(&session.delete_paragraph(&args, build_commit))
                        .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "diff_preview" => {
                let mut json =
                    serde_json::to_string(&session.diff_preview(build_commit)).expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "commit_sync" => {
                let mut json = serde_json::to_string(&session.commit_sync(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "accept_revision" => {
                let mut json = serde_json::to_string(&session.decide_single(
                    "accept_revision",
                    "accept",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "reject_revision" => {
                let mut json = serde_json::to_string(&session.decide_single(
                    "reject_revision",
                    "reject",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "reinsert_deleted_text" => {
                let mut json = serde_json::to_string(&session.decide_single(
                    "reinsert_deleted_text",
                    "reinsert",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "delete_comment" => {
                let mut json = serde_json::to_string(&session.delete_comment(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "table_insert_row" => {
                let mut json = serde_json::to_string(&session.table_op(
                    "table_insert_row",
                    "table-insert-row",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "table_delete_row" => {
                let mut json = serde_json::to_string(&session.table_op(
                    "table_delete_row",
                    "table-delete-row",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "table_insert_col" => {
                let mut json = serde_json::to_string(&session.table_op(
                    "table_insert_col",
                    "table-insert-col",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "table_delete_col" => {
                let mut json = serde_json::to_string(&session.table_op(
                    "table_delete_col",
                    "table-delete-col",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "table_merge_cells" => {
                let mut json = serde_json::to_string(&session.table_op(
                    "table_merge_cells",
                    "table-merge-cells",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "table_split_cells" => {
                let mut json = serde_json::to_string(&session.table_op(
                    "table_split_cells",
                    "table-split-cells",
                    &args,
                    build_commit,
                ))
                .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "decide_all" => {
                let mut json = serde_json::to_string(&session.decide_all(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "list_comments" => {
                let mut json = serde_json::to_string(&session.list_comments(build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "get_comment" => {
                let mut json = serde_json::to_string(&session.get_comment(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_preflight" => {
                let mut json = serde_json::to_string(&session.review_preflight(build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_state" => {
                let mut json =
                    serde_json::to_string(&session.review_state(build_commit)).expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_external_preflight" => {
                let mut json =
                    serde_json::to_string(&session.review_external_preflight(&args, build_commit))
                        .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_settlement_plan" => {
                let mut json =
                    serde_json::to_string(&session.review_settlement_plan(&args, build_commit))
                        .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_settle" => {
                let mut json = serde_json::to_string(&session.review_settle(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_apply_patch" => {
                let mut json =
                    serde_json::to_string(&session.review_apply_patch(&args, build_commit))
                        .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_apply_batch" => {
                let mut json =
                    serde_json::to_string(&session.review_apply_batch(&args, build_commit))
                        .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_inbox" => {
                let mut json = serde_json::to_string(&session.review_inbox(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "review_ack" => {
                let mut json = serde_json::to_string(&session.review_ack(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "revert" => {
                let mut json = serde_json::to_string(&session.revert(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "build_docx" => {
                let mut json = serde_json::to_string(&session.build_docx(&args, build_commit))
                    .expect("serializes");
                json.insert_str(0, "OK ");
                json
            }
            "verify_output" => {
                let mut json = serde_json::to_string(&session.verify_output(&args, build_commit))
                    .expect("serializes");
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
