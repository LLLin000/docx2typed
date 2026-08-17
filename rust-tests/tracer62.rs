//! Issue #62 binary-level tracer: representative real-document chain
//! (inspect -> non-destructive schema migration -> prose edit + review-lane
//! ops -> build -> independent verify) over the committed real-world-shaped
//! fixtures and their legacy Python workdirs, legacy immutability proof,
//! clean-cutover production-surface scan, and evidence-schema validation.
//!
//! Python-free by construction: the legacy workdirs are committed fixtures
//! (produced once by the Python Reference extract and registered in the
//! evidence provenance); the tests drive only the installed-style
//! `docx2typed` binary.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_docx2typed")
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn scratch(tag: &str) -> PathBuf {
    let dir =
        std::env::temp_dir().join(format!("docx2typed-tracer62-{tag}-{}", std::process::id()));
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

fn op_id(seed: &str) -> String {
    // Deterministic 32-hex operation id per step (stable retry semantics).
    let mut bytes = seed.as_bytes().to_vec();
    while bytes.len() < 32 {
        bytes.push(b'0');
    }
    bytes.truncate(32);
    String::from_utf8(bytes).expect("ascii")
}

fn zip_part_hashes(path: &Path) -> BTreeMap<String, String> {
    let file = std::fs::File::open(path).expect("open docx");
    let mut archive = zip::ZipArchive::new(file).expect("zip");
    let mut hashes = BTreeMap::new();
    for index in 0..archive.len() {
        let mut member = archive.by_index(index).expect("member");
        let name = member.name().to_string();
        let mut bytes = Vec::new();
        std::io::Read::read_to_end(&mut member, &mut bytes).expect("read");
        use sha2::{Digest, Sha256};
        hashes.insert(name, hex::encode(Sha256::digest(&bytes)));
    }
    hashes
}

fn tree_hashes(dir: &Path) -> BTreeMap<String, String> {
    let mut map = BTreeMap::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(current) = stack.pop() {
        for entry in std::fs::read_dir(&current).expect("read dir") {
            let entry = entry.expect("dir entry");
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else {
                let rel = path
                    .strip_prefix(dir)
                    .expect("within tree")
                    .to_string_lossy()
                    .replace('\\', "/");
                use sha2::{Digest, Sha256};
                let digest = Sha256::digest(std::fs::read(&path).expect("read file"));
                map.insert(rel, hex::encode(digest));
            }
        }
    }
    map
}

fn edits() -> Value {
    let path = repo_root().join("qualification/rust_tracer62/fixtures/edits.json");
    let text = std::fs::read_to_string(path).expect("read edits.json");
    serde_json::from_str(&text).expect("edits.json parses")
}

fn fixture_cfg<'a>(edits: &'a Value, name: &str) -> &'a Value {
    edits.get(name).expect("fixture config")
}

// ---------------------------------------------------------------------------
// Bullet 1 + 2: real-document chain + legacy immutability
// ---------------------------------------------------------------------------

fn run_chain(name: &str) {
    let edits = edits();
    let cfg = fixture_cfg(&edits, name);
    let dir = scratch(name);
    let legacy = repo_root().join(format!(
        "qualification/rust_tracer62/fixtures/legacy/{name}-workdir"
    ));
    assert!(
        legacy.is_dir(),
        "legacy workdir fixture missing: {legacy:?}"
    );

    let src = dir.join("src");
    copy_dir(&legacy, &src);
    let target = dir.join("target");
    let out_docx = dir.join("out.docx");

    let tree_before = tree_hashes(&src);
    let source_docx = repo_root().join(format!("corpus/release/{name}.docx"));
    let source_parts = zip_part_hashes(&source_docx);

    // inspect (read-only)
    let (rc, envelope) = rust_json(&["inspect", "--json", src.to_str().expect("utf8")]);
    assert_eq!(rc, 0, "inspect failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");
    assert_eq!(envelope["data"]["readiness"], "ready");

    // non-destructive schema migration
    let (rc, envelope) = rust_json(&[
        "migrate",
        "--json",
        src.to_str().expect("utf8"),
        "--out",
        target.to_str().expect("utf8"),
        "--operation-id",
        &op_id(&format!("{name}-migrate")),
    ]);
    assert_eq!(rc, 0, "migrate failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");
    let manifest: Value = serde_json::from_str(
        &std::fs::read_to_string(target.join("workdir.manifest.json")).expect("manifest"),
    )
    .expect("manifest parses");
    assert_eq!(manifest["schema"], "docx2typed-workdir-manifest-1");
    assert_eq!(manifest["producer"]["engine"], "docx2typed-rust");
    assert!(
        dir.join("target.migrate.evidence.json").is_file(),
        "migrate evidence sidecar missing"
    );

    // legacy immutability after migration
    assert_eq!(
        tree_hashes(&src),
        tree_before,
        "legacy source changed by migrate"
    );

    // prose edit on the migrated Rust workdir (generation commit)
    let leaf = cfg["leaf"].as_str().expect("leaf");
    let old_text = cfg["old"].as_str().expect("old");
    let new_text = cfg["new"].as_str().expect("new");
    let (rc, envelope) = rust_json(&[
        "edit",
        "text",
        "--json",
        target.to_str().expect("utf8"),
        leaf,
        old_text,
        new_text,
        "--operation-id",
        &op_id(&format!("{name}-edit")),
    ]);
    assert_eq!(rc, 0, "edit failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");
    assert_eq!(envelope["data"]["changed"][0], leaf);

    // review lane: revisions inventory + comment inventory (+ deletion)
    let (rc, envelope) = rust_json(&[
        "revisions",
        "list",
        "--json",
        target.to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 0, "revisions list failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");

    let (rc, envelope) = rust_json(&["comment", "list", "--json", target.to_str().expect("utf8")]);
    assert_eq!(rc, 0, "comment list failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");
    let comment_count = envelope["data"]["comments"]
        .as_array()
        .map(|a| a.len())
        .unwrap_or(0);
    if cfg["comment_delete"].as_bool().unwrap_or(false) {
        assert!(comment_count >= 2, "expected comments to delete");
        let (rc, envelope) = rust_json(&[
            "comment",
            "delete",
            "--json",
            target.to_str().expect("utf8"),
            "1",
            "--operation-id",
            &op_id(&format!("{name}-comment-delete")),
        ]);
        assert_eq!(rc, 0, "comment delete failed: {envelope}");
        assert_eq!(envelope["data"]["decision"]["action"], "comment-delete");
    } else {
        assert_eq!(comment_count, 0, "patent fixture has no comments");
    }

    // build
    let (rc, envelope) = rust_json(&[
        "build",
        "--json",
        target.to_str().expect("utf8"),
        "-o",
        out_docx.to_str().expect("utf8"),
        "--operation-id",
        &op_id(&format!("{name}-build")),
    ]);
    assert_eq!(rc, 0, "build failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");

    // independent verify
    let (rc, envelope) = rust_json(&[
        "verify",
        "--json",
        target.to_str().expect("utf8"),
        out_docx.to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 0, "verify failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");
    let verifier_checks = envelope["evidence"][0]["payload"]["verifier_checks"]
        .as_array()
        .expect("verifier_checks");
    assert!(
        verifier_checks
            .iter()
            .all(|check| check["status"] == "pass"),
        "all verifier checks pass: {verifier_checks:?}"
    );

    // byte preservation: only the expected parts change, nothing added/removed
    let built = zip_part_hashes(&out_docx);
    let mut expected_changed: Vec<String> = cfg["expect_changed"]
        .as_array()
        .expect("expect_changed")
        .iter()
        .map(|v| v.as_str().expect("part").to_string())
        .collect();
    expected_changed.sort();
    let changed: Vec<String> = source_parts
        .keys()
        .filter(|name| source_parts.get(*name) != built.get(*name))
        .cloned()
        .collect();
    assert_eq!(changed, expected_changed, "{name}: changed parts mismatch");
    assert_eq!(
        source_parts.len(),
        built.len(),
        "{name}: no parts added or removed"
    );

    // legacy immutability after the WHOLE chain (rollback asset intact)
    assert_eq!(
        tree_hashes(&src),
        tree_before,
        "legacy source changed by full chain"
    );
}

fn copy_dir(from: &Path, to: &Path) {
    std::fs::create_dir_all(to).expect("create target dir");
    for entry in std::fs::read_dir(from).expect("read source dir") {
        let entry = entry.expect("dir entry");
        let target = to.join(entry.file_name());
        if entry.path().is_dir() {
            copy_dir(&entry.path(), &target);
        } else {
            std::fs::copy(entry.path(), target).expect("copy file");
        }
    }
}

#[test]
fn patent_shaped_real_doc_chain_and_legacy_immutability() {
    run_chain("patent-shaped");
}

#[test]
fn paper_shaped_real_doc_chain_and_legacy_immutability() {
    run_chain("paper-shaped");
}

// ---------------------------------------------------------------------------
// Bullet 3: clean cutover - production surfaces have no Python fallback
// ---------------------------------------------------------------------------

#[test]
fn production_surfaces_have_no_python_fallback_resolver() {
    let root = repo_root();
    for name in [
        "SKILL.md",
        "Installation.md",
        "README.md",
        "README.zh-CN.md",
    ] {
        let text = std::fs::read_to_string(root.join(name)).expect(name);
        for (pattern, label) in [
            ("uvx\\s+docx2typed", "uvx docx2typed resolver"),
            ("python\\s+-m\\s+scripts\\s+", "python -m scripts fallback"),
            (
                "\"command\"\\s*:\\s*\"(python|uvx)",
                "MCP python/uvx command resolver",
            ),
            (
                "python\\s+-m\\s+docx2typed\\s+mcp",
                "python -m docx2typed mcp launcher",
            ),
        ] {
            let re = regex::Regex::new(pattern).expect("regex");
            assert!(
                !re.is_match(&text),
                "{name} still contains an active Python fallback resolver: {label}"
            );
        }
    }
    // The repo-root Python runtime installers are absent (production install
    // resolves only the signed Rust binary via scripts/install_binary.ps1).
    assert!(
        !root.join("install.ps1").exists(),
        "install.ps1 must be removed"
    );
    assert!(
        !root.join("install.sh").exists(),
        "install.sh must be removed"
    );
}

// ---------------------------------------------------------------------------
// Bullet 4: evidence schema validation (when the gate has produced it)
// ---------------------------------------------------------------------------

#[test]
fn tracer62_evidence_schema_when_present() {
    let path = repo_root().join("qualification/evidence/rust_tracer62_evidence.json");
    if !path.is_file() {
        // The PowerShell gate produces the evidence; unit tests must not
        // depend on gate state, but the CI gate job asserts it exists.
        return;
    }
    let text = std::fs::read_to_string(&path).expect("read evidence");
    let evidence: Value = serde_json::from_str(&text).expect("evidence parses");
    for key in [
        "schema",
        "issue",
        "branch",
        "generated",
        "host",
        "gate",
        "binary",
        "checks",
        "checks_pass",
        "checks_total",
        "docs_matrix",
        "legacy",
        "cutover",
        "office_matrix",
        "release_ready",
        "verdict",
        "deferrals",
    ] {
        assert!(evidence.get(key).is_some(), "evidence missing key {key}");
    }
    assert_eq!(evidence["release_ready"], Value::Bool(false));
    assert_eq!(evidence["office_matrix"]["status"], "not-run-no-host");
    assert_eq!(
        evidence["office_matrix"]["blocking_summary"]["gate"],
        "fail"
    );
    assert_eq!(evidence["cutover"]["resolver"], "rust-absolute-path-only");
    assert_eq!(
        evidence["cutover"]["python_launcher_in_tree"],
        Value::Bool(false)
    );
    for key in [
        "rollout_counts",
        "telemetry",
        "committee",
        "long_term_oracle_policy",
    ] {
        assert!(
            evidence["deferrals"].get(key).is_some(),
            "deferral missing {key}"
        );
    }
    assert_eq!(evidence["checks_pass"], evidence["checks_total"]);
}
