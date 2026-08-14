//! Synchronous, stateless Engine use-case seam (issue #36 resolution,
//! issue #55 slice): `Engine::execute(Operation, OperationContext) ->
//! OperationOutcome`. The Engine centrally performs workdir validation,
//! Core planning, Store commit/publish, the required independent Verifier
//! check, and Result/Diagnostic/Evidence construction. Adapters only
//! translate transport DTOs and invoke the Engine — they cannot reassemble
//! or bypass this order.
//!
//! The slice implements `extract`, no-op `build`, and `verify`; edits,
//! review, decisions, and recovery land in #56+.

use std::path::{Path, PathBuf};

use docx2typed_core::{plan_build, plan_extract, validate_workdir, Asset, BuildPlan, ChangeSet};
use docx2typed_protocol::{
    base_evidence_payload, file_sha256, resolve_path, run_evidence, typed_path_value, Diagnostic,
    ResultEnvelope, RunEvidence,
};
use docx2typed_store::{StoreError, WorkdirStore};
use docx2typed_verify::{IndependentVerifier, VerificationEvidence, VerificationRequest};

/// Closed operation set for the slice.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Operation {
    Extract,
    Build,
    Verify,
}

impl Operation {
    pub fn name(&self) -> &'static str {
        match self {
            Operation::Extract => "extract",
            Operation::Build => "build",
            Operation::Verify => "verify",
        }
    }

    /// The frozen finite-command set the CLI/MCP expose.
    pub const IMPLEMENTED_COMMANDS: [&str; 3] = ["extract", "build", "verify"];
}

/// Typed, adapter-validated operation arguments (adapters parse and convert
/// wire DTOs; serde optional/default behavior never defines domain
/// invariants).
#[derive(Clone, Debug)]
pub enum OperationArgs {
    Extract(ExtractArgs),
    Build(BuildArgs),
    Verify(VerifyArgs),
}

#[derive(Clone, Debug)]
pub struct ExtractArgs {
    pub input: PathBuf,
    pub outdir: PathBuf,
}

#[derive(Clone, Debug)]
pub struct BuildArgs {
    pub workdir: PathBuf,
    pub output: Option<PathBuf>,
}

#[derive(Clone, Debug)]
pub struct VerifyArgs {
    pub workdir: PathBuf,
    pub output: PathBuf,
}

#[derive(Clone, Debug)]
pub struct OperationContext {
    pub operation_id: String,
    /// Check profile (S / L / X); recorded in evidence. Enforcement of
    /// wall/RSS budgets is a measurement gate in this slice (issue #38
    /// gates), not an in-engine fail-closed limit.
    pub profile: String,
    #[allow(dead_code)]
    pub deadline: Option<std::time::Instant>,
}

impl OperationContext {
    pub fn new(operation_id: String) -> Self {
        OperationContext {
            operation_id,
            profile: "S".to_string(),
            deadline: None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Success,
    Failure,
    Partial,
}

impl Outcome {
    pub fn as_str(&self) -> &'static str {
        match self {
            Outcome::Success => "success",
            Outcome::Failure => "failure",
            Outcome::Partial => "partial",
        }
    }
}

/// One executed operation's Result/Diagnostic/Evidence construction.
#[derive(Clone, Debug)]
pub struct OperationOutcome {
    pub outcome: Outcome,
    pub data: serde_json::Value,
    pub diagnostics: Vec<Diagnostic>,
    pub evidence: Vec<RunEvidence>,
}

impl OperationOutcome {
    pub fn success(data: serde_json::Value, evidence: Vec<RunEvidence>) -> Self {
        OperationOutcome {
            outcome: Outcome::Success,
            data,
            diagnostics: Vec::new(),
            evidence,
        }
    }

    pub fn failure(diagnostics: Vec<Diagnostic>) -> Self {
        OperationOutcome {
            outcome: Outcome::Failure,
            data: serde_json::Value::Object(Default::default()),
            diagnostics,
            evidence: Vec::new(),
        }
    }

    /// Wrap into the `docx2typed-result-1` envelope with the live engine
    /// descriptor.
    pub fn into_envelope(self, operation: &str, build_commit: &str) -> ResultEnvelope {
        ResultEnvelope::new(
            operation,
            self.outcome.as_str(),
            self.data,
            self.diagnostics,
            self.evidence,
            build_commit,
        )
    }
}

/// Engine-level failure (a crash, not a domain failure): the adapter turns
/// this into a hard error. Domain failures are `OperationOutcome::failure`.
#[derive(Clone, Debug)]
pub struct EngineFailure {
    pub message: String,
}

// ---------------------------------------------------------------------------
// Ports (issue #36: app privately defines narrow StorePort/VerifierPort
// because each has two real adapters: the production concrete implementation
// and a deterministic in-memory test implementation).
// ---------------------------------------------------------------------------

pub trait StorePort {
    fn commit_workdir(&self, dir: &Path, change_set: &ChangeSet) -> Result<(), StoreError>;
    fn publish_build(&self, template: &Path, output: &Path) -> Result<PathBuf, StoreError>;
    fn publish_run_evidence(&self, path: &Path, evidence: &RunEvidence) -> Result<(), StoreError>;
    fn derived_workdir_manifest(&self, dir: &Path) -> serde_json::Value;
    fn manifest_sha256(&self, dir: &Path) -> String;
}

pub trait VerifierPort {
    fn verify(&self, request: &VerificationRequest) -> VerificationEvidence;
}

impl StorePort for WorkdirStore {
    fn commit_workdir(&self, dir: &Path, change_set: &ChangeSet) -> Result<(), StoreError> {
        WorkdirStore::commit_workdir(self, dir, change_set)
    }

    fn publish_build(&self, template: &Path, output: &Path) -> Result<PathBuf, StoreError> {
        WorkdirStore::publish_build(self, template, output)
    }

    fn publish_run_evidence(&self, path: &Path, evidence: &RunEvidence) -> Result<(), StoreError> {
        WorkdirStore::publish_run_evidence(self, path, evidence)
    }

    fn derived_workdir_manifest(&self, dir: &Path) -> serde_json::Value {
        WorkdirStore::derived_workdir_manifest(self, dir)
    }

    fn manifest_sha256(&self, dir: &Path) -> String {
        WorkdirStore::manifest_sha256(self, dir)
    }
}

impl VerifierPort for IndependentVerifier {
    fn verify(&self, request: &VerificationRequest) -> VerificationEvidence {
        IndependentVerifier::verify(self, request)
    }
}

/// Deterministic in-memory store for Engine tests.
#[derive(Default)]
pub struct MemoryStore {
    /// workdir path -> asset path -> bytes
    pub workdirs: std::cell::RefCell<
        std::collections::HashMap<PathBuf, std::collections::HashMap<String, Vec<u8>>>,
    >,
    pub builds: std::cell::RefCell<std::collections::HashMap<PathBuf, Vec<u8>>>,
    pub evidence: std::cell::RefCell<Vec<(PathBuf, RunEvidence)>>,
}

impl MemoryStore {
    fn workdir_dir(&self, dir: &Path) -> Option<std::collections::HashMap<String, Vec<u8>>> {
        self.workdirs.borrow().get(&resolve_path(dir)).cloned()
    }

    fn missing(message: &str) -> StoreError {
        StoreError::Io(std::io::Error::new(std::io::ErrorKind::NotFound, message))
    }
}

impl StorePort for MemoryStore {
    fn commit_workdir(&self, dir: &Path, change_set: &ChangeSet) -> Result<(), StoreError> {
        let mut assets = std::collections::HashMap::new();
        for asset in &change_set.assets {
            match asset {
                Asset::Bytes(path, bytes) => {
                    assets.insert(path.clone(), bytes.clone());
                }
                Asset::CopySource { path, source } => {
                    let bytes = std::fs::read(source).map_err(StoreError::Io)?;
                    assets.insert(path.clone(), bytes);
                }
            }
        }
        self.workdirs.borrow_mut().insert(resolve_path(dir), assets);
        Ok(())
    }

    fn publish_build(&self, template: &Path, output: &Path) -> Result<PathBuf, StoreError> {
        let template_dir = template.parent().unwrap_or_else(|| Path::new("."));
        let dir = self
            .workdir_dir(template_dir)
            .ok_or_else(|| MemoryStore::missing("memory store: missing workdir"))?;
        let bytes = dir
            .get("_template.docx")
            .ok_or_else(|| MemoryStore::missing("memory store: missing _template.docx"))?
            .clone();
        self.builds.borrow_mut().insert(resolve_path(output), bytes);
        Ok(resolve_path(output))
    }

    fn publish_run_evidence(&self, path: &Path, evidence: &RunEvidence) -> Result<(), StoreError> {
        self.evidence
            .borrow_mut()
            .push((resolve_path(path), evidence.clone()));
        Ok(())
    }

    fn derived_workdir_manifest(&self, dir: &Path) -> serde_json::Value {
        let mut assets = Vec::new();
        if let Some(workdir) = self.workdir_dir(dir) {
            for (name, bytes) in &workdir {
                assets.push(serde_json::json!({
                    "path": name,
                    "bytes": bytes.len(),
                    "sha256": docx2typed_protocol::bytes_sha256(bytes),
                }));
            }
        }
        serde_json::json!({
            "schema": "docx2typed-derived-workdir-manifest-1",
            "assets": assets,
        })
    }

    fn manifest_sha256(&self, dir: &Path) -> String {
        docx2typed_protocol::semantic_sha256(&self.derived_workdir_manifest(dir))
    }
}

/// Deterministic in-memory verifier for Engine tests: mirrors the
/// production checks against the memory store's bytes.
pub struct MemoryVerifier {
    pub store: std::rc::Rc<std::cell::RefCell<MemoryStore>>,
}

impl MemoryVerifier {
    pub fn new(store: std::rc::Rc<std::cell::RefCell<MemoryStore>>) -> Self {
        MemoryVerifier { store }
    }
}

impl VerifierPort for MemoryVerifier {
    fn verify(&self, request: &VerificationRequest) -> VerificationEvidence {
        let store = self.store.borrow();
        let template_bytes = store
            .workdirs
            .borrow()
            .get(&resolve_path(&request.workdir))
            .and_then(|dir| dir.get("_template.docx").cloned())
            .unwrap_or_default();
        let output_bytes = store
            .builds
            .borrow()
            .get(&resolve_path(&request.output))
            .cloned()
            .unwrap_or_default();
        let identical = !template_bytes.is_empty() && template_bytes == output_bytes;
        VerificationEvidence {
            verdict: if identical { "pass" } else { "fail" }.to_string(),
            checks: vec![docx2typed_verify::VerificationCheck {
                name: "parts-match-template".to_string(),
                status: if identical { "pass" } else { "fail" }.to_string(),
                detail: None,
            }],
            output_sha256: docx2typed_protocol::bytes_sha256(&output_bytes),
            template_sha256: docx2typed_protocol::bytes_sha256(&template_bytes),
            parts_identical: identical,
            profile: request.profile.clone(),
        }
    }
}

// ---------------------------------------------------------------------------
// Engine
// ---------------------------------------------------------------------------

pub struct Engine {
    store: Box<dyn StorePort>,
    verifier: Box<dyn VerifierPort>,
}

impl Engine {
    pub fn new() -> Self {
        Engine::with_ports(
            Box::new(WorkdirStore::new()),
            Box::new(IndependentVerifier::new()),
        )
    }

    pub fn with_ports(store: Box<dyn StorePort>, verifier: Box<dyn VerifierPort>) -> Self {
        Engine { store, verifier }
    }

    /// Execute one operation synchronously. Domain failures are
    /// `OperationOutcome::failure` carrying frozen Diagnostics; only
    /// unrecoverable engine faults return `Err(EngineFailure)`.
    pub fn execute(
        &self,
        operation: Operation,
        context: OperationContext,
        args: OperationArgs,
    ) -> Result<OperationOutcome, EngineFailure> {
        match operation {
            Operation::Extract => self.extract(&context, args),
            Operation::Build => self.build(&context, args),
            Operation::Verify => self.verify(&context, args),
        }
    }

    fn extract(
        &self,
        context: &OperationContext,
        args: OperationArgs,
    ) -> Result<OperationOutcome, EngineFailure> {
        let OperationArgs::Extract(args) = args else {
            return Err(EngineFailure {
                message: "operation/args mismatch".to_string(),
            });
        };
        let source = resolve_path(&args.input);
        if !source.is_file() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "input-not-found",
                format!("source file not found: {}", source.to_string_lossy()),
            )]));
        }
        let source_sha256 = match file_sha256(&source) {
            Ok(hash) => hash,
            Err(error) => {
                return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                    "workdir-unreadable",
                    error.to_string(),
                )]))
            }
        };
        let change_set = match plan_extract(&source, &args.outdir) {
            Ok(change_set) => change_set,
            Err(error) => return Ok(self.domain_failure("extract", &error.to_string())),
        };
        if let Err(error) = self.store.commit_workdir(&args.outdir, &change_set) {
            return Ok(self.domain_failure("extract", &error.to_string()));
        }
        let workdir = resolve_path(&args.outdir);
        let manifest_sha256 = self.store.manifest_sha256(&workdir);
        let payload = serde_json::json!({
            "engine": base_evidence_payload().get("engine"),
            "contracts": base_evidence_payload().get("contracts"),
            "inputs": {"source": {"sha256": source_sha256}},
            "outputs": {"workdir": {"manifest_sha256": manifest_sha256}},
            "checks": [{"name": "workdir-extracted", "status": "pass"}],
        });
        let evidence = run_evidence(
            "extract",
            "success",
            "mutation",
            &context.operation_id,
            payload,
        );
        let evidence_path = workdir.join("run.evidence.json");
        if let Err(error) = self.store.publish_run_evidence(&evidence_path, &evidence) {
            return Ok(OperationOutcome::failure(vec![Diagnostic::with_details(
                "evidence-publish-failed",
                format!("required run evidence could not be published: {error}"),
                None,
                None,
            )]));
        }
        Ok(OperationOutcome::success(
            serde_json::json!({ "workdir": typed_path_value(&workdir) }),
            vec![evidence],
        ))
    }

    fn build(
        &self,
        context: &OperationContext,
        args: OperationArgs,
    ) -> Result<OperationOutcome, EngineFailure> {
        let OperationArgs::Build(args) = args else {
            return Err(EngineFailure {
                message: "operation/args mismatch".to_string(),
            });
        };
        let workdir = resolve_path(&args.workdir);
        if !workdir.is_dir() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "workdir-not-found",
                format!("typed workdir not found: {}", workdir.to_string_lossy()),
            )]));
        }
        let manifest_before = self.store.manifest_sha256(&workdir);
        let plan: BuildPlan = match plan_build(&workdir) {
            Ok(plan) => plan,
            Err(error) => return Ok(self.domain_failure("build", &error.to_string())),
        };
        let output = match &args.output {
            Some(path) => resolve_path(path),
            None => {
                let name = workdir
                    .file_name()
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_else(|| "workdir".to_string());
                workdir
                    .parent()
                    .unwrap_or_else(|| Path::new("."))
                    .join(format!("{name}.docx"))
            }
        };
        if !plan.replay {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "workdir-invalid",
                "no-op slice cannot build edited workdirs".to_string(),
            )]));
        }
        let published = match self.store.publish_build(&plan.template, &output) {
            Ok(path) => path,
            Err(error) => return Ok(self.domain_failure("build", &error.to_string())),
        };
        let output_sha256 = match file_sha256(&published) {
            Ok(hash) => hash,
            Err(error) => return Ok(self.domain_failure("build", &error.to_string())),
        };
        let output_bytes = std::fs::metadata(&published)
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        let payload = serde_json::json!({
            "engine": base_evidence_payload().get("engine"),
            "contracts": base_evidence_payload().get("contracts"),
            "inputs": {"workdir": {"manifest_sha256": manifest_before}},
            "outputs": {"docx": {"sha256": output_sha256, "bytes": output_bytes}},
            "checks": [{"name": "build", "status": "pass"}],
        });
        let evidence = run_evidence("build", "success", "build", &context.operation_id, payload);
        let evidence_path = PathBuf::from(format!("{}.evidence.json", published.to_string_lossy()));
        if let Err(error) = self.store.publish_run_evidence(&evidence_path, &evidence) {
            return Ok(OperationOutcome::failure(vec![Diagnostic::with_details(
                "evidence-publish-failed",
                format!("required run evidence could not be published: {error}"),
                None,
                None,
            )]));
        }
        Ok(OperationOutcome::success(
            serde_json::json!({ "output": typed_path_value(&published) }),
            vec![evidence],
        ))
    }

    fn verify(
        &self,
        context: &OperationContext,
        args: OperationArgs,
    ) -> Result<OperationOutcome, EngineFailure> {
        let OperationArgs::Verify(args) = args else {
            return Err(EngineFailure {
                message: "operation/args mismatch".to_string(),
            });
        };
        let workdir = resolve_path(&args.workdir);
        if !workdir.is_dir() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "workdir-not-found",
                format!("typed workdir not found: {}", workdir.to_string_lossy()),
            )]));
        }
        let output = resolve_path(&args.output);
        if !output.is_file() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "input-not-found",
                format!("output DOCX not found: {}", output.to_string_lossy()),
            )]));
        }
        let manifest = self.store.manifest_sha256(&workdir);
        let verification = self.verifier.verify(&VerificationRequest {
            workdir: workdir.clone(),
            output: output.clone(),
            profile: context.profile.clone(),
        });
        if verification.verdict != "pass" {
            let detail = verification
                .checks
                .iter()
                .find(|check| check.status != "pass")
                .map(|check| check.name.clone())
                .unwrap_or_else(|| "verification".to_string());
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "workdir-invalid",
                format!("independent verification failed: {detail}"),
            )]));
        }
        let output_sha256 = verification.output_sha256.clone();
        let payload = serde_json::json!({
            "engine": base_evidence_payload().get("engine"),
            "contracts": base_evidence_payload().get("contracts"),
            "inputs": {"workdir": {"manifest_sha256": manifest}},
            "outputs": {"docx": {"sha256": output_sha256}},
            "verdict": "pass",
            "checks": [{"name": "independent-verification", "status": "pass"}],
        });
        let evidence = run_evidence(
            "verify",
            "success",
            "verify",
            &context.operation_id,
            payload,
        );
        let evidence_path =
            PathBuf::from(format!("{}.verify.evidence.json", output.to_string_lossy()));
        if let Err(error) = self.store.publish_run_evidence(&evidence_path, &evidence) {
            return Ok(OperationOutcome::failure(vec![Diagnostic::with_details(
                "evidence-publish-failed",
                format!("required run evidence could not be published: {error}"),
                None,
                None,
            )]));
        }
        Ok(OperationOutcome::success(
            serde_json::json!({ "verified": typed_path_value(&output) }),
            vec![evidence],
        ))
    }

    /// Validate a workdir and produce the `docx2typed-session-descriptor-1`
    /// payload (MCP `workdir_open` support). Domain errors return the
    /// failure text; the adapter maps it to a frozen Diagnostic.
    pub fn open_workdir_session(
        &self,
        workdir: &Path,
        author: Option<&str>,
    ) -> Result<serde_json::Value, String> {
        let meta = validate_workdir(workdir).map_err(|error| error.to_string())?;
        let manifest_sha256 = self.store.manifest_sha256(&meta.root);
        Ok(serde_json::json!({
            "schema": "docx2typed-session-descriptor-1",
            "workdir": typed_path_value(&meta.root),
            "workdir_manifest_sha256": manifest_sha256,
            "freshness": if meta.pristine { "clean" } else { "dirty" },
            "effective_mode": "direct",
            "author": author,
            "paragraphs": 0,
            "snapshot": {"current": "clean", "staged": "clean"},
            "cas": {"current_matches_filesystem": true},
            "supported_tools": docx2typed_protocol::PROTOCOL_TOOLS.to_vec(),
        }))
    }

    /// Map a Core/Store failure into a frozen Diagnostic. The message's
    /// kebab prefix is used as the code when registered; otherwise the
    /// stable `workdir-invalid` domain default applies (mirroring Python's
    /// `domain_code_from_message`).
    fn domain_failure(&self, _operation: &str, message: &str) -> OperationOutcome {
        let candidate = message
            .split(':')
            .next()
            .unwrap_or("")
            .trim()
            .to_lowercase()
            .replace(' ', "-");
        let code = match candidate.as_str() {
            "file not found" | "source file not found" => "input-not-found",
            "workdir not found" => "workdir-not-found",
            "not a valid docx"
            | "incompatible typed workdir schema"
            | "workdir missing"
            | "source-drift"
            | "invalid workdir json" => "workdir-invalid",
            other if is_registered_code(other) => other,
            _ => "workdir-invalid",
        };
        OperationOutcome::failure(vec![Diagnostic::new(code, message.to_string())])
    }
}

fn is_registered_code(code: &str) -> bool {
    matches!(
        code,
        "input-not-found"
            | "workdir-not-found"
            | "workdir-invalid"
            | "workdir-unreadable"
            | "contract-incompatible"
            | "required-feature-unsupported"
            | "evidence-publish-failed"
            | "resource-limit-exceeded"
            | "invalid-arguments"
    )
}

impl Default for Engine {
    fn default() -> Self {
        Engine::new()
    }
}
