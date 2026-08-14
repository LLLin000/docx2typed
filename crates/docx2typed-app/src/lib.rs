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
    Inspect,
    Migrate,
}

impl Operation {
    pub fn name(&self) -> &'static str {
        match self {
            Operation::Extract => "extract",
            Operation::Build => "build",
            Operation::Verify => "verify",
            Operation::Inspect => "inspect",
            Operation::Migrate => "migrate",
        }
    }

    /// The frozen finite-command set the CLI/MCP expose.
    pub const IMPLEMENTED_COMMANDS: [&str; 5] =
        ["extract", "build", "verify", "inspect", "migrate"];
}

/// Typed, adapter-validated operation arguments (adapters parse and convert
/// wire DTOs; serde optional/default behavior never defines domain
/// invariants).
#[derive(Clone, Debug)]
pub enum OperationArgs {
    Extract(ExtractArgs),
    Build(BuildArgs),
    Verify(VerifyArgs),
    Inspect(InspectArgs),
    Migrate(MigrateArgs),
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
pub struct InspectArgs {
    pub source: PathBuf,
}

#[derive(Clone, Debug)]
pub struct MigrateArgs {
    pub source: PathBuf,
    pub target: PathBuf,
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
    /// Byte-for-byte staged copy of a workdir (mtimes preserved).
    fn copy_workdir(&self, source: &Path, staging: &Path) -> Result<(), StoreError>;
    /// Write the versioned workdir manifest into the staging workdir.
    fn write_workdir_manifest(
        &self,
        staging: &Path,
        manifest: &serde_json::Value,
    ) -> Result<(), StoreError>;
    /// Atomically publish the staged workdir onto `target`.
    fn publish_workdir(&self, staging: &Path, target: &Path) -> Result<PathBuf, StoreError>;
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

    fn copy_workdir(&self, source: &Path, staging: &Path) -> Result<(), StoreError> {
        WorkdirStore::copy_workdir(self, source, staging)
    }

    fn write_workdir_manifest(
        &self,
        staging: &Path,
        manifest: &serde_json::Value,
    ) -> Result<(), StoreError> {
        WorkdirStore::write_workdir_manifest(self, staging, manifest)
    }

    fn publish_workdir(&self, staging: &Path, target: &Path) -> Result<PathBuf, StoreError> {
        WorkdirStore::publish_workdir(self, staging, target)
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

    // Migration is filesystem-only: the Engine's inspect/verify steps read
    // the real source directory, which the memory store never materializes.
    // These fail closed so a memory-backed Engine can never claim a migrate
    // that did not happen on disk.
    fn copy_workdir(&self, _source: &Path, _staging: &Path) -> Result<(), StoreError> {
        Err(StoreError::Io(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "memory store: migrate is filesystem-only",
        )))
    }

    fn write_workdir_manifest(
        &self,
        staging: &Path,
        manifest: &serde_json::Value,
    ) -> Result<(), StoreError> {
        let mut workdirs = self.workdirs.borrow_mut();
        let workdir = workdirs.entry(resolve_path(staging)).or_default();
        workdir.insert(
            "workdir.manifest.json".to_string(),
            serde_json::to_vec(manifest).expect("manifest serializes"),
        );
        Ok(())
    }

    fn publish_workdir(&self, staging: &Path, target: &Path) -> Result<PathBuf, StoreError> {
        let mut dirs = self.workdirs.borrow_mut();
        let staged = dirs
            .remove(&resolve_path(staging))
            .ok_or_else(|| MemoryStore::missing("memory store: missing staging workdir"))?;
        dirs.insert(resolve_path(target), staged);
        Ok(resolve_path(target))
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
            Operation::Inspect => self.inspect(&context, args),
            Operation::Migrate => self.migrate(&context, args),
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

    /// Read-only readiness classification of one schema-1 workdir
    /// (mirroring Python's `_inspect_json`): the classification payload is
    /// the Result data even when readiness is `blocked`.
    fn inspect(
        &self,
        _context: &OperationContext,
        args: OperationArgs,
    ) -> Result<OperationOutcome, EngineFailure> {
        let OperationArgs::Inspect(args) = args else {
            return Err(EngineFailure {
                message: "operation/args mismatch".to_string(),
            });
        };
        let source = resolve_path(&args.source);
        if !source.exists() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "workdir-not-found",
                format!("source workdir not found: {}", source.to_string_lossy()),
            )]));
        }
        if !source.is_dir() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "workdir-invalid",
                format!("source is not a directory: {}", source.to_string_lossy()),
            )]));
        }
        let inspection = match docx2typed_core::inspect::inspect_workdir(&source) {
            Ok(inspection) => inspection,
            Err(error) => {
                return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                    "workdir-unreadable",
                    error.to_string(),
                )]))
            }
        };
        Ok(OperationOutcome::success(inspection.payload, Vec::new()))
    }

    /// Lossless schema-1 -> manifest-backed migration (mirroring Python's
    /// `_migrate_json` + `migrate_workdir`): inspect, stage byte-for-byte
    /// in a sibling temp dir, verify asset closure + byte/semantic
    /// equivalence + typed validation + observable behavior, write the
    /// versioned manifest, publish the evidence sidecar, then atomically
    /// rename staging onto TARGET. Any failure removes staging and the
    /// sidecar and leaves no normal TARGET. The source is never modified.
    fn migrate(
        &self,
        context: &OperationContext,
        args: OperationArgs,
    ) -> Result<OperationOutcome, EngineFailure> {
        let OperationArgs::Migrate(args) = args else {
            return Err(EngineFailure {
                message: "operation/args mismatch".to_string(),
            });
        };
        let source = resolve_path(&args.source);
        let target = resolve_path(&args.target);
        if !source.exists() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "workdir-not-found",
                format!("source workdir not found: {}", source.to_string_lossy()),
            )]));
        }
        if !source.is_dir() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "workdir-invalid",
                format!("source is not a directory: {}", source.to_string_lossy()),
            )]));
        }
        let inspection = match docx2typed_core::inspect::inspect_workdir(&source) {
            Ok(inspection) => inspection,
            Err(error) => {
                return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                    "workdir-unreadable",
                    error.to_string(),
                )]))
            }
        };
        if inspection.readiness != "ready" {
            let reason = docx2typed_core::inspect::blocking_reason(&inspection.reason_codes);
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                reason,
                format!(
                    "source workdir is not migratable: {}",
                    inspection.reason_codes.join(", ")
                ),
            )]));
        }
        if target.exists() {
            return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                "target-already-exists",
                format!("target already exists: {}", target.to_string_lossy()),
            )]));
        }
        if let Some(parent) = target.parent() {
            if let Err(error) = std::fs::create_dir_all(parent) {
                return Ok(OperationOutcome::failure(vec![Diagnostic::new(
                    "workdir-unreadable",
                    error.to_string(),
                )]));
            }
        }
        let target_name = target
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "workdir".to_string());
        let staging = target
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(format!(
                ".{}.migrate-{}.tmp",
                target_name,
                docx2typed_protocol::new_operation_id()
            ));
        let evidence_path = PathBuf::from(format!(
            "{}.migrate.evidence.json",
            target.to_string_lossy()
        ));
        let result = self.migrate_attempt(
            &source,
            &target,
            &staging,
            &evidence_path,
            &inspection,
            &context.operation_id,
        );
        match result {
            Ok((data, evidence)) => Ok(OperationOutcome::success(data, vec![evidence])),
            Err((code, message)) => {
                // Failed publication leaves no normal TARGET: staging and
                // the evidence sidecar are removed.
                let _ = std::fs::remove_dir_all(&staging);
                let _ = std::fs::remove_file(&evidence_path);
                Ok(OperationOutcome::failure(vec![Diagnostic::new(
                    &code, message,
                )]))
            }
        }
    }

    /// The staged migration pipeline: copy -> verify -> manifest ->
    /// evidence -> atomic publish. Returns `(data, evidence)` on success or
    /// `(diagnostic code, message)` on any failure (the caller cleans up).
    fn migrate_attempt(
        &self,
        source: &Path,
        target: &Path,
        staging: &Path,
        evidence_path: &Path,
        inspection: &docx2typed_core::inspect::Inspection,
        operation_id: &str,
    ) -> Result<(serde_json::Value, RunEvidence), (String, String)> {
        let fail = |code: &str, message: String| Err((code.to_string(), message));
        if let Err(error) = std::fs::create_dir(staging) {
            return fail("workdir-unreadable", error.to_string());
        }
        if let Err(error) = self.store.copy_workdir(source, staging) {
            return fail("workdir-unreadable", error.to_string());
        }
        let checks = match verify_staged(self.store.as_ref(), source, staging, inspection) {
            Ok(checks) => checks,
            Err(message) => return fail("migrate-verification-failed", message),
        };
        let manifest = match build_manifest(
            self.store.as_ref(),
            source,
            staging,
            inspection,
            operation_id,
            &checks,
        ) {
            Ok(manifest) => manifest,
            Err(message) => return fail("migrate-verification-failed", message),
        };
        if let Err(error) = self.store.write_workdir_manifest(staging, &manifest) {
            return fail("workdir-unreadable", error.to_string());
        }
        let payload = evidence_payload(&manifest, &checks, inspection);
        let evidence = run_evidence("migrate", "success", "mutation", operation_id, payload);
        if let Err(error) = self.store.publish_run_evidence(evidence_path, &evidence) {
            return fail(
                "evidence-publish-failed",
                format!("required run evidence could not be published: {error}"),
            );
        }
        if let Err(error) = self.store.publish_workdir(staging, target) {
            return fail("workdir-unreadable", error.to_string());
        }
        let data = serde_json::json!({
            "operation_id": operation_id,
            "workdir": typed_path_value(target),
            "manifest": typed_path_value(&target.join("workdir.manifest.json")),
        });
        Ok((data, evidence))
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
            | "invalid workdir json" => "workdir-invalid",
            // source-drift is a registered code (Python emits it directly).
            "source-drift" => "source-drift",
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
            | "asset-closure"
            | "schema-incompatible"
            | "source-drift"
            | "symlink-detected"
            | "target-already-exists"
            | "operation-id-required"
            | "migrate-verification-failed"
            | "edit-dirty"
            | "edit-stale"
            | "edit-conflict"
            | "edit-state-missing"
            | "edit-state-incompatible"
            | "edit-binding-mismatch"
            | "edit-header-tampered"
            | "edit-grammar-invalid"
            | "edit-header-missing"
    )
}

// ---------------------------------------------------------------------------
// Migration helpers (mirroring scripts/inspect_migrate.py
// `_verify_staged` / `_build_manifest` / `_evidence_payload`)
// ---------------------------------------------------------------------------

/// Present (path -> entry) map over an inventory table.
fn present_assets(
    assets: &[docx2typed_core::inspect::AssetEntry],
) -> std::collections::BTreeMap<&str, &docx2typed_core::inspect::AssetEntry> {
    assets
        .iter()
        .filter(|asset| asset.presence == "present")
        .map(|asset| (asset.path.as_str(), asset))
        .collect()
}

/// Verify the staged copy before publication: asset closure, byte
/// equivalence, semantic equivalence (derived workdir manifest), typed
/// validation, and observable behavior (clean workdirs build byte-identical
/// output — the Rust mirror replays the template bytes).
fn verify_staged(
    store: &dyn StorePort,
    source: &Path,
    staging: &Path,
    inspection: &docx2typed_core::inspect::Inspection,
) -> Result<Vec<serde_json::Value>, String> {
    let mut checks = Vec::new();
    let source_assets = match docx2typed_core::inspect::inventory_assets(source) {
        Ok(assets) => assets,
        Err(error) => return Err(error.to_string()),
    };
    let staged_assets = match docx2typed_core::inspect::inventory_assets(staging) {
        Ok(assets) => assets,
        Err(error) => return Err(error.to_string()),
    };
    let source_present = present_assets(&source_assets);
    let staged_present = present_assets(&staged_assets);
    let missing: Vec<&str> = source_present
        .keys()
        .filter(|path| !staged_present.contains_key(*path))
        .cloned()
        .collect();
    let extra: Vec<&str> = staged_present
        .keys()
        .filter(|path| !source_present.contains_key(*path))
        .cloned()
        .collect();
    if !missing.is_empty() || !extra.is_empty() {
        return Err(format!(
            "asset closure mismatch after copy; missing={missing:?} extra={extra:?}"
        ));
    }
    let mismatched: Vec<&str> = source_present
        .iter()
        .filter(|(path, asset)| {
            staged_present
                .get(*path)
                .map(|staged| staged.sha256 != asset.sha256)
                .unwrap_or(true)
        })
        .map(|(path, _)| *path)
        .collect();
    if !mismatched.is_empty() {
        return Err(format!("asset bytes differ after copy: {mismatched:?}"));
    }
    checks.push(serde_json::json!({ "name": "asset-closure", "status": "pass" }));
    checks.push(serde_json::json!({ "name": "byte-equivalence", "status": "pass" }));

    if store.derived_workdir_manifest(source) != store.derived_workdir_manifest(staging) {
        return Err("semantic workdir manifest differs after copy".to_string());
    }
    checks.push(serde_json::json!({ "name": "semantic-equivalence", "status": "pass" }));

    if let Err(error) = docx2typed_core::validate_workdir(staging) {
        return Err(format!(
            "typed validation of the staged workdir failed: {error}"
        ));
    }
    checks.push(serde_json::json!({ "name": "typed-validation", "status": "pass" }));

    // Observable behavior equivalence: a clean workdir must build
    // byte-identical output from source and from the staged copy (the no-op
    // contract replays the template bytes). Non-clean semantic state is
    // preserved (never flattened) and recorded in the manifest instead.
    let edit_state = inspection
        .edit_state
        .get("state")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    if edit_state == "clean" {
        let source_hash = docx2typed_protocol::file_sha256(&source.join("_template.docx"))
            .map_err(|error| error.to_string())?;
        let staged_hash = docx2typed_protocol::file_sha256(&staging.join("_template.docx"))
            .map_err(|error| error.to_string())?;
        if source_hash != staged_hash {
            return Err(
                "observable build output differs between source and staged workdir".to_string(),
            );
        }
        checks.push(serde_json::json!({ "name": "behavior-equivalence", "status": "pass" }));
    } else {
        checks.push(serde_json::json!({ "name": "behavior-equivalence", "status": "skipped" }));
    }
    Ok(checks)
}

/// The versioned workdir manifest (`docx2typed-workdir-manifest-1`),
/// mirroring `_build_manifest`: present assets minus the manifest file plus
/// the single generated self-entry (null self-hash).
fn build_manifest(
    store: &dyn StorePort,
    source: &Path,
    staging: &Path,
    inspection: &docx2typed_core::inspect::Inspection,
    operation_id: &str,
    checks: &[serde_json::Value],
) -> Result<serde_json::Value, String> {
    use docx2typed_core::inspect::MANIFEST_FILE;
    let format_data = docx2typed_core::inspect::format_data(source).unwrap_or_default();
    let mut assets = Vec::new();
    for asset in &inspection.assets {
        if asset.presence != "present" || asset.path == MANIFEST_FILE {
            continue;
        }
        assets.push(asset.to_json());
    }
    assets.push(serde_json::json!({
        "path": MANIFEST_FILE,
        "kind": "generated",
        "required": true,
        "read_only": true,
        "role": "workdir-manifest",
        "presence": "present",
        "bytes": serde_json::Value::Null,
        "sha256": serde_json::Value::Null,
        "mtime_ns": serde_json::Value::Null,
    }));
    let identity =
        docx2typed_core::inspect::inventory_sha256(source).map_err(|error| error.to_string())?;
    Ok(serde_json::json!({
        "schema": docx2typed_core::inspect::WORKDIR_MANIFEST_SCHEMA,
        "manifest_version": docx2typed_core::inspect::MANIFEST_VERSION,
        "workdir_schema": format_data.get("schema").and_then(serde_json::Value::as_str).unwrap_or("typed-format-1"),
        "model_version": format_data.get("model_version").and_then(serde_json::Value::as_i64).unwrap_or(1),
        "canonicalizer_version": format_data.get("canonicalizer_version").and_then(serde_json::Value::as_i64).unwrap_or(1),
        "producer": {
            "engine": docx2typed_protocol::ENGINE_NAME,
            "version": docx2typed_protocol::PACKAGE_VERSION,
            "operation": "migrate",
            "operation_id": operation_id,
        },
        "source": {
            "identity": identity,
            "semantic_manifest_sha256": store.manifest_sha256(source),
        },
        "baseline": {
            "template": format_data.get("template"),
            "template_sha256": format_data.get("template_sha256"),
            "package_manifest": format_data.get("package_manifest"),
            "source": format_data.get("source"),
            "source_sha256": format_data.get("source_sha256"),
            "styles_sha256": format_data.get("styles_sha256"),
            "document_xml_sha256": format_data.get("document_xml_sha256"),
            "source_track_enabled": format_data.get("source_track_enabled"),
            "uses_date_utc": format_data.get("uses_date_utc"),
        },
        "features": {
            "supported": docx2typed_protocol::FEATURES,
            "required": docx2typed_protocol::REQUIRED_FEATURES,
        },
        "state": {
            "readiness": inspection.readiness,
            "edit": inspection.edit_state,
            "revision_count": inspection.revision_count,
            "comment_count": inspection.comment_count,
            "reason_codes": inspection.reason_codes,
            "semantic_manifest_sha256": store.manifest_sha256(staging),
        },
        "checks": checks,
        "assets": assets,
    }))
}

/// The semantic part of the migrate run-evidence record, mirroring
/// `_evidence_payload` (producer engine identity comes from the live
/// `base_evidence_payload`).
fn evidence_payload(
    manifest: &serde_json::Value,
    checks: &[serde_json::Value],
    inspection: &docx2typed_core::inspect::Inspection,
) -> serde_json::Value {
    let base = base_evidence_payload();
    let opaque = inspection
        .assets
        .iter()
        .filter(|asset| asset.kind == "opaque" && asset.presence == "present")
        .count();
    serde_json::json!({
        "engine": base.get("engine"),
        "contracts": base.get("contracts"),
        "inputs": {
            "source": {
                "inventory_sha256": manifest.get("source").and_then(|v| v.get("identity")),
                "semantic_manifest_sha256": manifest.get("source").and_then(|v| v.get("semantic_manifest_sha256")),
            }
        },
        "outputs": {
            "target": {
                "manifest_sha256": docx2typed_protocol::semantic_sha256(manifest),
                "semantic_manifest_sha256": manifest.get("state").and_then(|v| v.get("semantic_manifest_sha256")),
                "assets": manifest.get("assets").and_then(serde_json::Value::as_array).map(|list| list.len()).unwrap_or(0),
                "opaque_assets": opaque,
            }
        },
        "checks": checks,
    })
}

impl Default for Engine {
    fn default() -> Self {
        Engine::new()
    }
}
