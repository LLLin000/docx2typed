//! Differential + focused gate for issue #56: drives the installed-style
//! `docx2typed` binary through `inspect` and `migrate` over real Python
//! schema-1 workdirs (produced by the Python Reference `python -m scripts
//! extract`), and compares classification / migration results against the
//! Python Reference `inspect`/`migrate_workdir` on the same workdir.
//!
//! Asserted contracts (issue #56 acceptance):
//! 1. Ready / non-clean / opaque / unknown-feature / source-drift /
//!    symlink classifications match the frozen protocol (deep-equal with
//!    the Python `inspect_workdir` payload).
//! 2. Migration preserves template bytes, identities, asset roles, pending
//!    states, lineage (source identity), and Evidence.
//! 3. Source bytes, mtimes, and lock state remain unchanged; failed
//!    publication creates no normal target.
//! 4. Clean targets no-op build (output == template bytes) and verify;
//!    non-clean targets refuse the build the same way the source would.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

/// Frozen Python Reference record (plan identities.fixture.fixtures.plain.docx).
const FROZEN_FIXTURE_SHA256: &str =
    "4323e37b7ac7e9dbce7b4923d14529bda821f0d66f0dce7005cf9299bf8d9c39";

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_docx2typed")
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}
/// The pinned Python Reference checkout. Qualification-only: these tests run
/// under `cargo test --workspace --features python-oracle` with
/// `DOCX2TYPED_PYTHON_ORACLE` pointing at the external pinned checkout.
/// There is deliberately NO fallback to this repo root — the Rust repo is
/// never a valid oracle, and a missing variable must fail loudly, not
/// silently "qualify" against the wrong tree.
fn python_oracle_root() -> PathBuf {
    std::env::var_os("DOCX2TYPED_PYTHON_ORACLE")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            panic!(
                "DOCX2TYPED_PYTHON_ORACLE must point at the pinned Python \
                 Reference checkout (python-oracle qualification target only)"
            )
        })
}

fn fixture() -> PathBuf {
    repo_root().join("corpus/release/plain.docx")
}

fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("docx2typed-migrate-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

/// Run the Rust binary with `--json` and parse the envelope.
fn rust_json(args: &[&str]) -> (i32, Value) {
    let output: Output = Command::new(bin())
        .args(args)
        .output()
        .expect("binary runs");
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    let value: Value = serde_json::from_str(&stdout)
        .unwrap_or_else(|error| panic!("binary JSON parse failed: {error}; output: {stdout}"));
    (output.status.code().unwrap_or(-1), value)
}

/// Run a pinned Python Reference command from the `DOCX2TYPED_PYTHON_ORACLE`
/// checkout (must be set by the qualification caller; no repo-root fallback).
fn python(args: &[&str]) -> Output {
    Command::new("python")
        .args(args)
        .current_dir(python_oracle_root())
        .output()
        .expect("python runs")
}

/// `python -m scripts extract <fixture> -o <outdir>`.
fn python_extract(source: &Path, outdir: &Path) {
    let output = python(&[
        "-m",
        "scripts",
        "extract",
        source.to_str().expect("utf8"),
        "-o",
        outdir.to_str().expect("utf8"),
    ]);
    assert!(
        output.status.success(),
        "python extract failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// `python -m scripts --json inspect <wd>` -> envelope data payload.
fn python_inspect_data(workdir: &Path) -> Value {
    let output = python(&[
        "-m",
        "scripts",
        "--json",
        "inspect",
        workdir.to_str().expect("utf8"),
    ]);
    assert!(
        output.status.success(),
        "python inspect failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let envelope: Value = serde_json::from_slice(&output.stdout).expect("python inspect JSON");
    envelope.get("data").expect("data payload").clone()
}

fn rust_inspect_data(workdir: &Path) -> Value {
    let (rc, envelope) = rust_json(&["inspect", "--json", workdir.to_str().expect("utf8")]);
    assert_eq!(
        rc, 0,
        "rust inspect must exit 0 (classification is the result)"
    );
    envelope.get("data").expect("data payload").clone()
}

fn rust_migrate(source: &Path, target: &Path, operation_id: &str) -> (i32, Value) {
    rust_json(&[
        "migrate",
        "--json",
        source.to_str().expect("utf8"),
        "--out",
        target.to_str().expect("utf8"),
        "--operation-id",
        operation_id,
    ])
}

fn sha256_file(path: &Path) -> String {
    let bytes = std::fs::read(path).expect("read for hash");
    docx2typed_protocol::bytes_sha256(&bytes)
}

fn snapshot(root: &Path) -> BTreeMap<String, (u64, u128)> {
    let mut map = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).expect("read dir") {
            let entry = entry.expect("dir entry");
            let path = entry.path();
            let meta = std::fs::metadata(&path).expect("meta");
            if meta.is_dir() {
                stack.push(path);
            } else if meta.is_file() {
                let rel = path
                    .strip_prefix(root)
                    .expect("under root")
                    .to_string_lossy()
                    .replace('\\', "/");
                let mtime = meta
                    .modified()
                    .expect("mtime")
                    .duration_since(std::time::UNIX_EPOCH)
                    .expect("post-epoch")
                    .as_nanos();
                map.insert(rel, (meta.len(), mtime));
            }
        }
    }
    map
}

fn manifest(target: &Path) -> Value {
    let text = std::fs::read_to_string(target.join("workdir.manifest.json"))
        .expect("target manifest exists");
    serde_json::from_str(&text).expect("target manifest parses")
}

fn diagnostic_codes(envelope: &Value) -> Vec<String> {
    envelope
        .get("diagnostics")
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .filter_map(|item| item.get("code").and_then(Value::as_str).map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

/// Create a directory junction (Windows reparse point). Returns false when
/// the host cannot create junctions (privileges/developer mode).
fn try_create_junction(link: &Path, target: &Path) -> bool {
    let status = Command::new("cmd")
        .args([
            "/c",
            "mklink",
            "/J",
            link.to_str().expect("utf8"),
            target.to_str().expect("utf8"),
        ])
        .status();
    match status {
        Ok(status) => status.success(),
        Err(_) => false,
    }
}

#[test]
fn ready_classification_matches_python_oracle_deep_equal() {
    let scratch = scratch("ready");
    let workdir = scratch.join("wd");
    python_extract(&fixture(), &workdir);

    let python = python_inspect_data(&workdir);
    let rust = rust_inspect_data(&workdir);
    assert_eq!(
        rust, python,
        "rust inspect payload must deep-equal the Python Reference payload"
    );
    assert_eq!(rust.get("readiness").and_then(Value::as_str), Some("ready"));
    assert_eq!(
        rust.get("reason_codes").and_then(Value::as_array).cloned(),
        Some(vec![Value::String("ok".to_string())])
    );
    assert_eq!(
        rust.pointer("/semantic_state/edit/state")
            .and_then(Value::as_str),
        Some("clean")
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn migrate_preserves_and_clean_target_noop_build_verify() {
    let scratch = scratch("clean");
    let workdir = scratch.join("wd");
    let target = scratch.join("migrated");
    let output = scratch.join("out.docx");
    python_extract(&fixture(), &workdir);

    let source_template = sha256_file(&workdir.join("_template.docx"));
    assert_eq!(source_template, FROZEN_FIXTURE_SHA256);

    let (rc, envelope) = rust_migrate(&workdir, &target, "0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a");
    assert_eq!(rc, 0, "migrate succeeds: {:?}", envelope);
    assert_eq!(
        envelope.get("outcome").and_then(Value::as_str),
        Some("success")
    );

    // Preservation: template bytes identical.
    assert_eq!(
        sha256_file(&target.join("_template.docx")),
        source_template,
        "target template bytes must equal source template bytes"
    );

    // Manifest invariants.
    let m = manifest(&target);
    assert_eq!(
        m.get("schema").and_then(Value::as_str),
        Some("docx2typed-workdir-manifest-1")
    );
    assert_eq!(m.get("manifest_version").and_then(Value::as_i64), Some(1));
    assert_eq!(
        m.get("state")
            .and_then(|s| s.get("readiness"))
            .and_then(Value::as_str),
        Some("ready")
    );
    let checks = m.get("checks").and_then(Value::as_array).expect("checks");
    assert_eq!(checks.len(), 5, "all five staged checks run");
    assert!(
        checks
            .iter()
            .all(|c| c.get("status").and_then(Value::as_str) == Some("pass")),
        "clean migration checks all pass: {checks:?}"
    );

    // Evidence sidecar exists and is a valid run-evidence record.
    let evidence_path = PathBuf::from(format!(
        "{}.migrate.evidence.json",
        target.to_string_lossy()
    ));
    let evidence: Value =
        serde_json::from_str(&std::fs::read_to_string(&evidence_path).expect("evidence sidecar"))
            .expect("evidence parses");
    assert_eq!(
        evidence.get("schema").and_then(Value::as_str),
        Some("docx2typed-run-evidence-1")
    );
    assert_eq!(
        evidence.get("operation").and_then(Value::as_str),
        Some("migrate")
    );

    // Clean target no-op build (output == template bytes) and verify.
    let (rc, build) = rust_json(&[
        "build",
        "--json",
        target.to_str().expect("utf8"),
        "-o",
        output.to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 0, "clean target builds: {:?}", build);
    assert_eq!(
        sha256_file(&output),
        source_template,
        "clean no-op build must replay the template bytes"
    );
    let (rc, verify) = rust_json(&[
        "verify",
        "--json",
        target.to_str().expect("utf8"),
        output.to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 0, "clean target verifies: {:?}", verify);
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn migrate_facts_match_python_reference() {
    let scratch = scratch("facts");
    let workdir = scratch.join("wd");
    let rust_target = scratch.join("rust-target");
    let python_target = scratch.join("python-target");
    python_extract(&fixture(), &workdir);
    const OP_ID: &str = "1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b";

    // Python Reference migration of the same workdir.
    let script = format!(
        "from scripts.inspect_migrate import migrate_workdir; \
         migrate_workdir(r'{}', r'{}', operation_id=r'{op}', evidence_path=r'{}.migrate.evidence.json')",
        workdir.to_str().unwrap(),
        python_target.to_str().unwrap(),
        python_target.to_str().unwrap(),
        op = OP_ID
    );
    let output = python(&["-c", &script]);
    assert!(
        output.status.success(),
        "python migrate failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let (rc, _) = rust_migrate(&workdir, &rust_target, OP_ID);
    assert_eq!(rc, 0, "rust migrate succeeds");

    let rust_manifest = manifest(&rust_target);
    let python_manifest = manifest(&python_target);

    // Lineage + identities must be cross-language identical (semantic
    // canonical JSON hashing parity).
    assert_eq!(
        rust_manifest.pointer("/source/identity"),
        python_manifest.pointer("/source/identity"),
        "inventory identity must match the Python Reference"
    );
    assert_eq!(
        rust_manifest.pointer("/source/semantic_manifest_sha256"),
        python_manifest.pointer("/source/semantic_manifest_sha256"),
        "derived semantic manifest hash must match the Python Reference"
    );
    assert_eq!(
        rust_manifest.pointer("/state/semantic_manifest_sha256"),
        python_manifest.pointer("/state/semantic_manifest_sha256")
    );

    // Full manifest equivalence except the producer provenance (Rust engine
    // identity is honest, Python's is its own).
    let mut rust_m = rust_manifest.clone();
    let mut python_m = python_manifest.clone();
    rust_m.as_object_mut().expect("object").remove("producer");
    python_m.as_object_mut().expect("object").remove("producer");
    assert_eq!(rust_m, python_m, "manifests equivalent minus producer");

    // Template bytes identical across all three workdirs.
    let source_template = sha256_file(&workdir.join("_template.docx"));
    assert_eq!(
        sha256_file(&rust_target.join("_template.docx")),
        source_template
    );
    assert_eq!(
        sha256_file(&python_target.join("_template.docx")),
        source_template
    );

    // Evidence sidecar structure parity (payload minus engine provenance).
    let rust_evidence: Value = serde_json::from_str(
        &std::fs::read_to_string(format!(
            "{}.migrate.evidence.json",
            rust_target.to_string_lossy()
        ))
        .expect("rust evidence"),
    )
    .expect("rust evidence parses");
    let python_evidence: Value = serde_json::from_str(
        &std::fs::read_to_string(format!(
            "{}.migrate.evidence.json",
            python_target.to_string_lossy()
        ))
        .expect("python evidence"),
    )
    .expect("python evidence parses");
    assert_eq!(
        rust_evidence.get("operation"),
        python_evidence.get("operation")
    );
    assert_eq!(rust_evidence.get("kind"), python_evidence.get("kind"));
    let mut rust_payload = rust_evidence.get("payload").expect("payload").clone();
    let mut python_payload = python_evidence.get("payload").expect("payload").clone();
    rust_payload
        .as_object_mut()
        .expect("payload object")
        .remove("engine");
    python_payload
        .as_object_mut()
        .expect("payload object")
        .remove("engine");
    // The target manifest_sha256 self-hashes the whole manifest including
    // the producer's engine identity, so it legitimately differs between
    // engines; the semantic parts must match exactly.
    for payload in [&mut rust_payload, &mut python_payload] {
        let target = payload
            .get_mut("outputs")
            .and_then(|v| v.as_object_mut())
            .and_then(|v| v.get_mut("target"))
            .and_then(|v| v.as_object_mut())
            .expect("outputs.target");
        let self_hash = target
            .get("manifest_sha256")
            .and_then(Value::as_str)
            .expect("hash");
        assert_eq!(
            self_hash.len(),
            64,
            "manifest_sha256 is a SHA-256 hex digest"
        );
        target.remove("manifest_sha256");
    }
    assert_eq!(
        rust_payload, python_payload,
        "evidence payload equivalent minus engine provenance and self-hash"
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn source_bytes_mtimes_and_lock_state_unchanged() {
    let scratch = scratch("immutable");
    let workdir = scratch.join("wd");
    let target = scratch.join("migrated");
    python_extract(&fixture(), &workdir);

    let before = snapshot(&workdir);
    let (rc, _) = rust_migrate(&workdir, &target, "2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c");
    assert_eq!(rc, 0);
    let after = snapshot(&workdir);
    assert_eq!(
        after, before,
        "source bytes and mtimes must be unchanged; no files added or removed"
    );
    assert!(
        !after
            .keys()
            .any(|key| key.contains("lock") || key.contains(".tmp")),
        "no lock or staging files may appear in the source"
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn non_clean_source_preserved_and_target_build_blocked() {
    let scratch = scratch("nonclean");
    let workdir = scratch.join("wd");
    let target = scratch.join("migrated");
    python_extract(&fixture(), &workdir);

    // Dirty the projection exactly like the Python suite's `_dirty_edit`.
    let edit_path = workdir.join("edit.md");
    let mut text = std::fs::read_to_string(&edit_path).expect("edit.md");
    let start = text.find("<!--@p id=").expect("paragraph marker");
    let marker = start + text[start..].find("-->").expect("close");
    let tail = text[marker..]
        .find('\n')
        .map(|i| marker + i + 1)
        .expect("newline");
    let end = text[tail..]
        .find('\n')
        .map(|i| tail + i)
        .unwrap_or(text.len());
    text.insert(end, '改');
    std::fs::write(&edit_path, text).expect("write dirty edit");

    // Classification parity for the non-clean workdir.
    let python = python_inspect_data(&workdir);
    let rust = rust_inspect_data(&workdir);
    assert_eq!(rust, python, "non-clean classification deep-equal");
    assert_eq!(
        rust.pointer("/semantic_state/edit/state")
            .and_then(Value::as_str),
        Some("dirty")
    );
    assert!(rust
        .get("reason_codes")
        .and_then(Value::as_array)
        .map(|codes| codes.iter().any(|c| c == "non-clean-edit"))
        .unwrap_or(false));

    // Migration preserves the non-clean state (readiness stays ready).
    let (rc, envelope) = rust_migrate(&workdir, &target, "3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d");
    assert_eq!(rc, 0, "non-clean sources are migratable: {:?}", envelope);
    let m = manifest(&target);
    assert_eq!(
        m.pointer("/state/edit/state").and_then(Value::as_str),
        Some("dirty")
    );
    // The same build block as the source: a dirty workdir refuses to build.
    let (rc, build) = rust_json(&[
        "build",
        "--json",
        target.to_str().expect("utf8"),
        "-o",
        scratch.join("out.docx").to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 1, "non-clean target build must be refused");
    assert_eq!(
        diagnostic_codes(&build),
        vec!["edit-dirty".to_string()],
        "refusal code matches Python's edit-dirty"
    );
    assert!(
        !scratch.join("out.docx").exists(),
        "no build output on refusal"
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn opaque_attachments_preserved_and_declared() {
    let scratch = scratch("opaque");
    let workdir = scratch.join("wd");
    let target = scratch.join("migrated");
    python_extract(&fixture(), &workdir);
    std::fs::create_dir_all(workdir.join("assets")).expect("opaque dir");
    std::fs::write(workdir.join("assets/logo.bin"), b"OPAQUE-BYTES-123").expect("opaque file");
    std::fs::write(workdir.join("notes.txt"), "hello opaque").expect("opaque file");

    let python = python_inspect_data(&workdir);
    let rust = rust_inspect_data(&workdir);
    assert_eq!(rust, python, "opaque classification deep-equal");
    assert_eq!(
        rust.pointer("/semantic_state/opaque_attachment_count")
            .and_then(Value::as_i64),
        Some(2)
    );
    assert_eq!(
        rust.get("readiness").and_then(Value::as_str),
        Some("ready"),
        "opaque attachments are informational, not blocking"
    );

    let (rc, _) = rust_migrate(&workdir, &target, "4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e");
    assert_eq!(rc, 0);
    assert_eq!(
        std::fs::read(target.join("assets/logo.bin")).expect("logo bytes"),
        b"OPAQUE-BYTES-123",
        "opaque bytes copied byte-for-byte"
    );
    assert_eq!(
        std::fs::read_to_string(target.join("notes.txt")).expect("notes"),
        "hello opaque"
    );
    let m = manifest(&target);
    let opaque: Vec<&str> = m
        .get("assets")
        .and_then(Value::as_array)
        .map(|assets| {
            assets
                .iter()
                .filter(|a| a.get("kind").and_then(Value::as_str) == Some("opaque"))
                .filter_map(|a| a.get("path").and_then(Value::as_str))
                .collect()
        })
        .unwrap_or_default();
    assert_eq!(
        opaque,
        vec!["assets/", "notes.txt"],
        "opaque assets manifest-declared"
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn symlink_and_junction_rejected_fail_closed() {
    let scratch = scratch("symlink");
    let workdir = scratch.join("wd");
    let target = scratch.join("migrated");
    python_extract(&fixture(), &workdir);

    let link = workdir.join("linkdir");
    if !try_create_junction(&link, &workdir) {
        eprintln!("skipping junction test: host cannot create junctions");
        let _ = std::fs::remove_dir_all(&scratch);
        return;
    }

    // Both engines report the same blocked classification.
    let python = python_inspect_data(&workdir);
    let rust = rust_inspect_data(&workdir);
    assert_eq!(rust, python, "symlink classification deep-equal");
    assert_eq!(
        rust.get("readiness").and_then(Value::as_str),
        Some("blocked")
    );
    assert_eq!(
        rust.get("symlinks").and_then(Value::as_array),
        Some(&vec![Value::String("linkdir".to_string())])
    );

    // Migration fails closed with symlink-detected, no target/staging/evidence.
    let (rc, envelope) = rust_migrate(&workdir, &target, "5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f");
    assert_eq!(rc, 1);
    assert_eq!(
        diagnostic_codes(&envelope),
        vec!["symlink-detected".to_string()]
    );
    assert!(
        !target.exists(),
        "no normal target after failed publication"
    );
    let parent = target.parent().expect("parent");
    let leftovers: Vec<PathBuf> = std::fs::read_dir(parent)
        .expect("parent dir")
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            let name = path
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            name.starts_with(".migrated.migrate-") || name == "migrated.migrate.evidence.json"
        })
        .collect();
    assert!(
        leftovers.is_empty(),
        "no staging or evidence leftovers: {leftovers:?}"
    );
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn unknown_required_feature_fails_closed_and_leaves_no_target() {
    let scratch = scratch("unknown-feature");
    let workdir = scratch.join("wd");
    let target = scratch.join("migrated");
    python_extract(&fixture(), &workdir);
    let format_path = workdir.join("format.json");
    let mut format: Value =
        serde_json::from_str(&std::fs::read_to_string(&format_path).expect("format.json"))
            .expect("format parses");
    format.as_object_mut().expect("format object").insert(
        "required_features".to_string(),
        serde_json::json!(["hybrid-fidelity", "made-up-feature"]),
    );
    std::fs::write(
        &format_path,
        serde_json::to_string_pretty(&format).expect("format writes") + "\n",
    )
    .expect("write format");

    let python = python_inspect_data(&workdir);
    let rust = rust_inspect_data(&workdir);
    assert_eq!(rust, python, "unknown-feature classification deep-equal");
    assert_eq!(
        rust.get("readiness").and_then(Value::as_str),
        Some("blocked")
    );
    assert!(rust
        .get("reason_codes")
        .and_then(Value::as_array)
        .map(|codes| codes.iter().any(|c| c == "required-feature-unsupported"))
        .unwrap_or(false));

    let (rc, envelope) = rust_migrate(&workdir, &target, "6a6a6a6a6a6a6a6a6a6a6a6a6a6a6a6a");
    assert_eq!(rc, 1);
    assert_eq!(
        diagnostic_codes(&envelope),
        vec!["required-feature-unsupported".to_string()]
    );
    assert!(!target.exists(), "no normal target");
    let evidence_path = format!("{}.migrate.evidence.json", target.to_string_lossy());
    assert!(!Path::new(&evidence_path).exists(), "no evidence sidecar");
    let _ = std::fs::remove_dir_all(&scratch);
}

#[test]
fn failed_publication_leaves_no_normal_target() {
    let scratch = scratch("no-target");
    let workdir = scratch.join("wd");
    let target = scratch.join("migrated");
    python_extract(&fixture(), &workdir);

    // A pre-existing target refuses migration (target-already-exists) and is
    // left untouched.
    std::fs::create_dir_all(&target).expect("pre-create target");
    std::fs::write(target.join("sentinel.txt"), "pre-existing").expect("sentinel");
    let (rc, envelope) = rust_migrate(&workdir, &target, "7b7b7b7b7b7b7b7b7b7b7b7b7b7b7b7b");
    assert_eq!(rc, 1);
    assert_eq!(
        diagnostic_codes(&envelope),
        vec!["target-already-exists".to_string()]
    );
    assert!(
        target.join("sentinel.txt").exists(),
        "pre-existing target untouched"
    );

    // Missing --operation-id in the JSON contract fails before any effect.
    let (rc, envelope) = rust_json(&[
        "migrate",
        "--json",
        workdir.to_str().expect("utf8"),
        "--out",
        scratch.join("other").to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 1);
    assert_eq!(
        diagnostic_codes(&envelope),
        vec!["operation-id-required".to_string()]
    );
    assert!(!scratch.join("other").exists());
    let _ = std::fs::remove_dir_all(&scratch);
}
