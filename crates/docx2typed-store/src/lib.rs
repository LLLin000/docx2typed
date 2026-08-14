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

    /// Byte-for-byte copy of every present asset into `staging` (file
    /// mtimes preserved, mirroring `shutil.copy2`). The inventory walk is
    /// link-safe, so migration can never read through a symlink/junction.
    pub fn copy_workdir(&self, source: &Path, staging: &Path) -> Result<(), StoreError> {
        let source = resolve_path(source);
        let staging = resolve_path(staging);
        fs::create_dir_all(&staging)?;
        let assets = docx2typed_core::inspect::inventory_assets(&source)
            .map_err(|error| StoreError::Io(std::io::Error::other(error.to_string())))?;
        for asset in assets {
            if asset.presence != "present" {
                continue;
            }
            if asset.path.ends_with('/') {
                copy_dir_preserving_mtimes(&source.join(&asset.path), &staging.join(&asset.path))?;
                continue;
            }
            let src = source.join(&asset.path);
            let dst = staging.join(&asset.path);
            if let Some(parent) = dst.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::copy(&src, &dst)?;
            preserve_mtime(&src, &dst)?;
        }
        Ok(())
    }

    /// Write the versioned workdir manifest into the staging workdir
    /// (`workdir.manifest.json`, indent-2 JSON + trailing newline).
    pub fn write_workdir_manifest(
        &self,
        staging: &Path,
        manifest: &serde_json::Value,
    ) -> Result<(), StoreError> {
        let mut json = serde_json::to_string_pretty(manifest).expect("manifest serializes");
        json.push('\n');
        fs::write(staging.join("workdir.manifest.json"), json)?;
        Ok(())
    }

    /// Atomically publish the staged workdir onto `target` (directory
    /// rename; the staging dir must be a sibling of `target` so the rename
    /// stays on one volume). A failed publish leaves no target.
    pub fn publish_workdir(&self, staging: &Path, target: &Path) -> Result<PathBuf, StoreError> {
        let target = resolve_path(target);
        fs::rename(staging, &target)?;
        Ok(target)
    }
}

fn preserve_mtime(src: &Path, dst: &Path) -> Result<(), StoreError> {
    let mtime = fs::metadata(src)?.modified()?;
    let file = fs::OpenOptions::new().write(true).open(dst)?;
    file.set_modified(mtime)?;
    Ok(())
}

fn copy_dir_preserving_mtimes(src: &Path, dst: &Path) -> Result<(), StoreError> {
    fs::create_dir_all(dst)?;
    for file in docx2typed_core::inspect::walk_files(src) {
        let rel = file
            .strip_prefix(src)
            .map_err(|_| StoreError::Io(std::io::Error::other("outside root")))?;
        let target = dst.join(rel);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(&file, &target)?;
        preserve_mtime(&file, &target)?;
    }
    Ok(())
}

fn asset_path(asset: &Asset) -> String {
    match asset {
        Asset::Bytes(path, _) => path.clone(),
        Asset::CopySource { path, .. } => path.clone(),
    }
}
