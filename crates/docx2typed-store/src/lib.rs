//! Deep module: workdir commit, byte-copy build publish, run-evidence and
//! manifest publication (issue #55 slice). Owns asset roles and hashes and
//! atomic JSON publication, but no Word/edit/review meaning.
//!
//! The no-op publish path is a pure byte copy (`fs::copy`) of the template
//! package — copy-if-unchanged per frozen PRD decisions 13/14/15. It never
//! recompresses.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use docx2typed_core::Asset;
use docx2typed_protocol::{bytes_sha256, file_sha256, resolve_path, semantic_sha256, RunEvidence};

/// Workdir asset set from the Python Reference (`_WORKDIR_ASSETS`).
pub const WORKDIR_ASSETS: [&str; 6] = [
    "_template.docx",
    "edit.md",
    "edit.state.json",
    "format.json",
    "styles.json",
    "typed.md",
];

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Debug)]
pub enum StoreError {
    Io(std::io::Error),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(error) => formatter.write_str(&error.to_string()),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<std::io::Error> for StoreError {
    fn from(error: std::io::Error) -> Self {
        StoreError::Io(error)
    }
}

/// Production store: real filesystem workdirs and atomic JSON publication.
pub struct WorkdirStore;

impl Default for WorkdirStore {
    fn default() -> Self {
        Self::new()
    }
}

impl WorkdirStore {
    pub fn new() -> Self {
        WorkdirStore
    }

    /// Materialize an extract ChangeSet into `dir` (created if absent).
    pub fn commit_workdir(
        &self,
        dir: &Path,
        change_set: &docx2typed_core::ChangeSet,
    ) -> Result<(), StoreError> {
        let root = resolve_path(dir);
        fs::create_dir_all(&root)?;
        for asset in &change_set.assets {
            let target = root.join(asset_path(asset));
            match asset {
                Asset::Bytes(_, bytes) => {
                    fs::write(&target, bytes)?;
                }
                Asset::CopySource { source, .. } => {
                    // byte copy: the template is the source package verbatim
                    fs::copy(source, &target)?;
                }
            }
        }
        Ok(())
    }

    /// Publish a build: byte-copy `template` to `output` (copy-if-unchanged,
    /// never recompression). Writes to a same-directory temp file first so a
    /// failed build never leaves a partial output, then renames into place.
    pub fn publish_build(&self, template: &Path, output: &Path) -> Result<PathBuf, StoreError> {
        let output = resolve_path(output);
        let parent = output.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent)?;
        let counter = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let temp = parent.join(format!(
            ".docx2typed-build-{}-{}.tmp",
            std::process::id(),
            counter
        ));
        fs::copy(template, &temp)?;
        match fs::rename(&temp, &output) {
            Ok(()) => {}
            Err(_) => {
                // Windows: rename fails when the destination exists; replace
                // it (atomic replace is a #50 recovery concern).
                let _ = fs::remove_file(&output);
                fs::rename(&temp, &output)?;
            }
        }
        Ok(output)
    }

    /// `docx2typed-derived-workdir-manifest-1` for the workdir (assets that
    /// exist, with bytes + sha256), mirroring Python's
    /// `derived_workdir_manifest`.
    pub fn derived_workdir_manifest(&self, dir: &Path) -> serde_json::Value {
        let root = resolve_path(dir);
        let mut assets = Vec::new();
        for name in WORKDIR_ASSETS {
            let path = root.join(name);
            if path.is_file() {
                let bytes = fs::read(&path).unwrap_or_default();
                assets.push(serde_json::json!({
                    "path": name,
                    "bytes": bytes.len(),
                    "sha256": bytes_sha256(&bytes),
                }));
            }
        }
        serde_json::json!({
            "schema": "docx2typed-derived-workdir-manifest-1",
            "assets": assets,
        })
    }

    pub fn manifest_sha256(&self, dir: &Path) -> String {
        semantic_sha256(&self.derived_workdir_manifest(dir))
    }

    /// Atomically persist one run-evidence record beside the operation's
    /// artifact (temp write + rename), mirroring `publish_run_evidence`.
    pub fn publish_run_evidence(
        &self,
        path: &Path,
        evidence: &RunEvidence,
    ) -> Result<(), StoreError> {
        let target = resolve_path(path);
        let parent = target.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent)?;
        let counter = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let temp = parent.join(format!(
            ".{}.{}-{}.tmp",
            target
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_else(|| "evidence".to_string()),
            std::process::id(),
            counter
        ));
        let mut json = serde_json::to_string_pretty(evidence).expect("evidence serializes");
        json.push('\n');
        fs::write(&temp, json)?;
        match fs::rename(&temp, &target) {
            Ok(()) => {}
            Err(_) => {
                let _ = fs::remove_file(&target);
                fs::rename(&temp, &target)?;
            }
        }
        Ok(())
    }

    /// Per-asset hashes of the workdir assets (for evidence payloads and
    /// the gate script).
    pub fn asset_hashes(&self, dir: &Path) -> BTreeMap<String, String> {
        let root = resolve_path(dir);
        let mut hashes = BTreeMap::new();
        for name in WORKDIR_ASSETS {
            let path = root.join(name);
            if path.is_file() {
                if let Ok(sha) = file_sha256(&path) {
                    hashes.insert(name.to_string(), sha);
                }
            }
        }
        hashes
    }
}

fn asset_path(asset: &Asset) -> String {
    match asset {
        Asset::Bytes(path, _) => path.clone(),
        Asset::CopySource { path, .. } => path.clone(),
    }
}
