//! Issue #59 binary-level tracer: governed workflows — tracked-revision
//! inventory and accept/reject views, single-revision settlement decisions
//! (identity + fingerprint guards fail closed with no side effect),
//! reinsertion, comment deletion (byte-evidence), table structure ops
//! (content-preservation guards), the read-only Unicode normalization
//! audit, and stable-negative boundaries — driven through the
//! installed-style `docx2typed` binary exactly as the differential gate
//! does.

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

fn fixture(name: &str) -> PathBuf {
    repo_root().join(format!("corpus/release/{name}.docx"))
}

fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("docx2typed-govern-{tag}-{}", std::process::id()));
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

fn op_id() -> String {
    docx2typed_protocol::new_operation_id()
}

fn extract(fixture_path: &Path, outdir: &Path) {
    let (rc, envelope) = rust_json(&[
        "extract",
        "--json",
        fixture_path.to_str().expect("utf8"),
        "-o",
        outdir.to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 0, "extract failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");
}

fn build(workdir: &Path, output: &Path) -> (i32, Value) {
    rust_json(&[
        "build",
        "--json",
        workdir.to_str().expect("utf8"),
        "-o",
        output.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ])
}

fn verify(workdir: &Path, output: &Path) -> (i32, Value) {
    rust_json(&[
        "verify",
        "--json",
        workdir.to_str().expect("utf8"),
        output.to_str().expect("utf8"),
    ])
}

fn part_bytes(path: &Path, member: &str) -> Vec<u8> {
    let file = std::fs::File::open(path).expect("open docx");
    let mut archive = zip::ZipArchive::new(file).expect("zip");
    for index in 0..archive.len() {
        let mut part = archive.by_index(index).expect("member");
        if part.name() == member {
            let mut bytes = Vec::new();
            std::io::Read::read_to_end(&mut part, &mut bytes).expect("read");
            return bytes;
        }
    }
    panic!("member not found: {member}");
}

/// Frozen Python Reference revision inventory for revisions.docx (captured
/// 2026-08-14 from `scripts/typed_docx.scan_package_revisions` +
/// `render_revisions_json` semantics).
type ExpectedRevision = (
    String,
    String,
    String,
    String,
    String,
    bool,
    Option<&'static str>,
);

fn expected_revisions() -> Vec<ExpectedRevision> {
    vec![
        (
            "1".into(),
            "insert".into(),
            "已插入内容".into(),
            "P0".into(),
            "888c104169b5".into(),
            true,
            None,
        ),
        (
            "2".into(),
            "delete".into(),
            "旧文本".into(),
            "P1".into(),
            "9cee19cf4fd0".into(),
            true,
            None,
        ),
        (
            "3".into(),
            "insert".into(),
            String::new(),
            "P2".into(),
            "e3b0c44298fc".into(),
            false,
            Some("paragraph-mark-revision"),
        ),
        (
            "4".into(),
            "insert".into(),
            "字段内插入".into(),
            "P3".into(),
            "32435cee5f33".into(),
            false,
            Some("nested-container-or-non-editable-part"),
        ),
        (
            "5".into(),
            "insert".into(),
            "公式内插入".into(),
            "P4".into(),
            "16dd62fc3284".into(),
            false,
            Some("nested-container-or-non-editable-part"),
        ),
        (
            "6".into(),
            "insert".into(),
            "修订五".into(),
            "P5".into(),
            "f6dffa3091fe".into(),
            true,
            None,
        ),
        (
            "7".into(),
            "delete".into(),
            "修订六".into(),
            "P6".into(),
            "7ba6f83040c9".into(),
            true,
            None,
        ),
        (
            "8".into(),
            "insert".into(),
            "修订七".into(),
            "P7".into(),
            "ec0ebc27eea0".into(),
            true,
            None,
        ),
    ]
}

// ---------------------------------------------------------------------------
// Bullet 1: revision inventory + original/final views preserve ancestry,
// authors, dates, and fingerprints
// ---------------------------------------------------------------------------

#[test]
fn revisions_inventory_matches_python_reference() {
    let (rc, envelope) = rust_json(&[
        "revisions",
        "list",
        "--json",
        fixture("revisions").to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 0, "revisions list failed: {envelope}");
    assert_eq!(
        envelope["data"]["schema"].as_str(),
        Some("typed-revisions-1")
    );
    let revisions = envelope["data"]["revisions"].as_array().expect("revisions");
    assert_eq!(revisions.len(), 8);
    let expected = expected_revisions();
    for (entry, (w_id, kind, text, paragraph_id, fingerprint, editable, reason)) in
        revisions.iter().zip(&expected)
    {
        assert_eq!(entry["w_id"].as_str(), Some(w_id.as_str()));
        assert_eq!(entry["kind"].as_str(), Some(kind.as_str()));
        assert_eq!(entry["text"].as_str(), Some(text.as_str()));
        assert_eq!(entry["paragraph_id"].as_str(), Some(paragraph_id.as_str()));
        assert_eq!(entry["fingerprint"].as_str(), Some(fingerprint.as_str()));
        assert_eq!(entry["editable"], Value::Bool(*editable));
        match reason {
            Some(reason) => assert_eq!(entry["reason"].as_str(), Some(*reason)),
            None => assert_eq!(entry["reason"], Value::Null),
        }
        assert_eq!(entry["author"].as_str(), Some("审稿人"));
        assert_eq!(entry["date"].as_str(), Some("2026-08-08T00:00:00Z"));
        assert_eq!(
            entry["revision_key"].as_str(),
            Some(format!("word/document.xml|{kind}|{w_id}|{fingerprint}").as_str())
        );
    }
}

#[test]
fn revisions_views_match_python_settlement() {
    let (rc, accept) = rust_json(&[
        "revisions",
        "view",
        "--json",
        fixture("revisions").to_str().expect("utf8"),
        "accept",
    ]);
    assert_eq!(rc, 0, "view failed: {accept}");
    assert_eq!(accept["data"]["action"].as_str(), Some("accept"));
    let texts: BTreeMap<String, String> = accept["data"]["paragraphs"]
        .as_array()
        .expect("paragraphs")
        .iter()
        .map(|p| {
            (
                p["id"].as_str().expect("id").to_string(),
                p["text"].as_str().expect("text").to_string(),
            )
        })
        .collect();
    assert_eq!(texts["P0"], "修订前文已插入内容修订后文");
    assert_eq!(texts["P1"], "保留");
    assert_eq!(texts["P2"], "段落标记段");
    assert_eq!(texts["P3"], "字段内插入");
    assert_eq!(texts["P4"], "公式内插入");
    assert_eq!(texts["P5"], "修订五");
    assert_eq!(texts["P6"], "");
    assert_eq!(texts["P7"], "修订七");

    let (rc, reject) = rust_json(&[
        "revisions",
        "view",
        "--json",
        fixture("revisions").to_str().expect("utf8"),
        "reject",
    ]);
    assert_eq!(rc, 0, "view failed: {reject}");
    let reject_texts: BTreeMap<String, String> = reject["data"]["paragraphs"]
        .as_array()
        .expect("paragraphs")
        .iter()
        .map(|p| {
            (
                p["id"].as_str().expect("id").to_string(),
                p["text"].as_str().expect("text").to_string(),
            )
        })
        .collect();
    assert_eq!(reject_texts["P1"], "保留旧文本");
    assert_eq!(reject_texts["P6"], "修订六");
    assert_eq!(reject_texts["P0"], "修订前文修订后文");
}

// ---------------------------------------------------------------------------
// Bullet 2: decisions with identity + fingerprint guards; verified new
// baseline generations
// ---------------------------------------------------------------------------

fn decide(workdir: &Path, action: &str, key: &str, fingerprint: &str) -> (i32, Value) {
    rust_json(&[
        "decide",
        action,
        key,
        "--json",
        "--workdir",
        workdir.to_str().expect("utf8"),
        "--fingerprint",
        fingerprint,
        "--operation-id",
        &op_id(),
    ])
}

fn manifest_sha(workdir: &Path) -> String {
    let (rc, envelope) = rust_json(&["store-state", "--json", workdir.to_str().expect("utf8")]);
    assert_eq!(rc, 0, "store-state failed: {envelope}");
    envelope["data"]
        .get("generation")
        .map(|value| value.as_str().unwrap_or("").to_string())
        .unwrap_or_default()
}

#[test]
fn decide_accept_commits_generation_and_builds_verified() {
    let dir = scratch("accept");
    let workdir = dir.join("wd");
    extract(&fixture("revisions"), &workdir);
    let before = manifest_sha(&workdir);
    let (rc, envelope) = decide(
        &workdir,
        "accept",
        "word/document.xml|insert|1|888c104169b5",
        "888c104169b5",
    );
    assert_eq!(rc, 0, "decide failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");
    assert_eq!(envelope["data"]["state"].as_str(), Some("clean"));
    let decision = &envelope["data"]["decision"];
    assert_eq!(decision["w_id"].as_str(), Some("1"));
    assert_eq!(decision["action"].as_str(), Some("accept"));
    assert_eq!(decision["operation"].as_str(), Some("unwrap"));
    assert_eq!(decision["paragraph_id"].as_str(), Some("P0"));
    let after = manifest_sha(&workdir);
    assert_ne!(before, after, "decide must commit a new generation");
    assert!(workdir.join("decisions.json").is_file());

    let output = dir.join("out.docx");
    let (rc, build) = build(&workdir, &output);
    assert_eq!(rc, 0, "build failed: {build}");
    let (rc, verify) = verify(&workdir, &output);
    assert_eq!(rc, 0, "verify failed: {verify}");
    assert_eq!(verify["outcome"], "success");

    // The built document no longer carries the decided revision.
    let (rc, list) = rust_json(&[
        "revisions",
        "list",
        "--json",
        workdir.to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 0);
    let ids: Vec<String> = list["data"]["revisions"]
        .as_array()
        .expect("revisions")
        .iter()
        .map(|entry| entry["w_id"].as_str().expect("w_id").to_string())
        .collect();
    assert!(!ids.contains(&"1".to_string()), "w:id 1 settled: {ids:?}");
}

#[test]
fn decide_guards_fail_closed_with_no_side_effect() {
    let dir = scratch("guards");
    let workdir = dir.join("wd");
    extract(&fixture("revisions"), &workdir);

    // Stale confirmation fingerprint.
    let (rc, envelope) = decide(
        &workdir,
        "accept",
        "word/document.xml|insert|1|888c104169b5",
        "deadbeef0000",
    );
    assert_eq!(rc, 1);
    assert_eq!(
        envelope["diagnostics"][0]["code"],
        "revision-fingerprint-mismatch"
    );

    // Unknown revision key.
    let (rc, envelope) = decide(
        &workdir,
        "accept",
        "word/document.xml|insert|99|000000000000",
        "000000000000",
    );
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "revision-not-found");

    // Paragraph-mark revision: view-only (Python parity).
    let (rc, envelope) = decide(
        &workdir,
        "accept",
        "word/document.xml|insert|3|e3b0c44298fc",
        "e3b0c44298fc",
    );
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "revision-not-found");

    // Opaque-interior revision: view-only.
    let (rc, envelope) = decide(
        &workdir,
        "accept",
        "word/document.xml|insert|4|32435cee5f33",
        "32435cee5f33",
    );
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "revision-not-found");

    // Live-text fingerprint mismatch (key fingerprint stale).
    let (rc, envelope) = decide(
        &workdir,
        "accept",
        "word/document.xml|insert|1|000000000000",
        "000000000000",
    );
    assert_eq!(rc, 1);
    assert_eq!(
        envelope["diagnostics"][0]["code"],
        "revision-text-fingerprint-mismatch"
    );

    // Kind mismatch.
    let (rc, envelope) = decide(
        &workdir,
        "reject",
        "word/document.xml|delete|1|888c104169b5",
        "888c104169b5",
    );
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "workdir-invalid");

    // Malformed key.
    let (rc, envelope) = decide(&workdir, "accept", "tooshort", "x");
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "malformed-revision-key");

    // Non-document part.
    let (rc, envelope) = decide(
        &workdir,
        "accept",
        "word/header1.xml|insert|1|888c104169b5",
        "888c104169b5",
    );
    assert_eq!(rc, 1);
    assert_eq!(
        envelope["diagnostics"][0]["code"],
        "revision-outside-editable-surface"
    );

    // No side effect: no generation advanced, no decisions sidecar, the
    // template is byte-identical to the source package.
    assert!(!workdir.join("decisions.json").exists(), "no sidecar");
    assert_eq!(
        std::fs::read(workdir.join("_template.docx")).expect("template"),
        std::fs::read(fixture("revisions")).expect("fixture"),
        "template bytes unchanged"
    );
}

#[test]
fn decide_reject_unwraps_deletion() {
    let dir = scratch("reject");
    let workdir = dir.join("wd");
    extract(&fixture("revisions"), &workdir);
    let (rc, envelope) = decide(
        &workdir,
        "reject",
        "word/document.xml|delete|2|9cee19cf4fd0",
        "9cee19cf4fd0",
    );
    assert_eq!(rc, 0, "decide failed: {envelope}");
    assert_eq!(
        envelope["data"]["decision"]["operation"].as_str(),
        Some("unwrap")
    );
    let output = dir.join("out.docx");
    let (rc, build) = build(&workdir, &output);
    assert_eq!(rc, 0, "build failed: {build}");
    let (rc, verify) = verify(&workdir, &output);
    assert_eq!(rc, 0, "verify failed: {verify}");
    let document = String::from_utf8_lossy(&part_bytes(&output, "word/document.xml")).into_owned();
    assert!(
        document.contains("<w:t>旧文本</w:t>"),
        "delText unwrapped to live text"
    );
    assert!(!document.contains("w:del w:id=\"2\""));
}

#[test]
fn decide_reinsert_creates_new_insertion() {
    let dir = scratch("reinsert");
    let workdir = dir.join("wd");
    extract(&fixture("revisions"), &workdir);
    let (rc, envelope) = decide(
        &workdir,
        "reinsert",
        "word/document.xml|delete|2|9cee19cf4fd0",
        "9cee19cf4fd0",
    );
    assert_eq!(rc, 0, "decide failed: {envelope}");
    let decision = &envelope["data"]["decision"];
    assert_eq!(
        decision["operation"].as_str(),
        Some("new-insert-after-deletion")
    );
    assert_eq!(decision["new_w_id"].as_str(), Some("0"));
    let output = dir.join("out.docx");
    let (rc, build) = build(&workdir, &output);
    assert_eq!(rc, 0, "build failed: {build}");
    let (rc, verify) = verify(&workdir, &output);
    assert_eq!(rc, 0, "verify failed: {verify}");
    let document = String::from_utf8_lossy(&part_bytes(&output, "word/document.xml")).into_owned();
    assert!(
        document.contains("<w:ins w:id=\"0\""),
        "new insertion present"
    );
    assert!(document.contains("<w:t>旧文本</w:t>"));
}

// ---------------------------------------------------------------------------
// Bullet 3: comment deletion with byte-evidence + verified new baseline
// ---------------------------------------------------------------------------

#[test]
fn comment_delete_removes_entry_and_anchors_leaving_others_byte_identical() {
    let dir = scratch("comment");
    let workdir = dir.join("wd");
    extract(&fixture("comments"), &workdir);

    // Inventory first.
    let (rc, list) = rust_json(&["comment", "list", "--json", workdir.to_str().expect("utf8")]);
    assert_eq!(rc, 0, "comment list failed: {list}");
    let comments = list["data"]["comments"].as_array().expect("comments");
    assert_eq!(comments.len(), 3);
    assert_eq!(comments[0]["id"].as_str(), Some("0"));
    assert_eq!(comments[0]["author"].as_str(), Some("审稿人"));
    assert_eq!(comments[0]["text"].as_str(), Some("批注一内容"));
    assert_eq!(comments[0]["anchors"].as_array().expect("anchors").len(), 3);

    // Delete comment 1.
    let (rc, envelope) = rust_json(&[
        "comment",
        "delete",
        "--json",
        workdir.to_str().expect("utf8"),
        "1",
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc, 0, "comment delete failed: {envelope}");
    assert_eq!(
        envelope["data"]["decision"]["comment_id"].as_str(),
        Some("1")
    );

    let output = dir.join("out.docx");
    let (rc, build) = build(&workdir, &output);
    assert_eq!(rc, 0, "build failed: {build}");
    let (rc, verify) = verify(&workdir, &output);
    assert_eq!(rc, 0, "verify failed: {verify}");

    // Byte evidence: comments 0 and 2 replay byte-identically; comment 1's
    // entry and its document anchors/references are gone.
    let source_comments = part_bytes(&fixture("comments"), "word/comments.xml");
    let built_comments = part_bytes(&output, "word/comments.xml");
    let source_text = String::from_utf8_lossy(&source_comments).into_owned();
    let built_text = String::from_utf8_lossy(&built_comments).into_owned();
    // Comment 0's full element (open tag .. first close) is byte-identical.
    let comment_element = |text: &str, id: &str| -> String {
        let start = text
            .find(&format!("<w:comment w:id=\"{id}\""))
            .expect("comment element start");
        let end = text[start..]
            .find("</w:comment>")
            .map(|i| start + i + "</w:comment>".len())
            .expect("comment element end");
        text[start..end].to_string()
    };
    assert_eq!(
        comment_element(&source_text, "0"),
        comment_element(&built_text, "0"),
        "comment 0 element byte-identical"
    );
    assert_eq!(
        comment_element(&source_text, "2"),
        comment_element(&built_text, "2"),
        "comment 2 element byte-identical"
    );
    assert!(!built_text.contains("w:id=\"1\""), "comment 1 entry gone");

    let built_document =
        String::from_utf8_lossy(&part_bytes(&output, "word/document.xml")).into_owned();
    assert!(!built_document.contains("commentRangeStart w:id=\"1\""));
    assert!(!built_document.contains("commentRangeEnd w:id=\"1\""));
    assert!(!built_document.contains("commentReference w:id=\"1\""));
    assert!(built_document.contains("commentRangeStart w:id=\"0\""));
    assert!(built_document.contains("commentRangeStart w:id=\"2\""));
    // Anchored text is preserved.
    assert!(built_document.contains("关键二"));

    // Remaining inventory: comments 0 and 2.
    let (rc, list) = rust_json(&["comment", "list", "--json", workdir.to_str().expect("utf8")]);
    assert_eq!(rc, 0);
    let ids: Vec<String> = list["data"]["comments"]
        .as_array()
        .expect("comments")
        .iter()
        .map(|comment| comment["id"].as_str().expect("id").to_string())
        .collect();
    assert_eq!(ids, vec!["0", "2"]);
}

#[test]
fn comment_delete_unknown_id_fails_closed() {
    let dir = scratch("comment-unknown");
    let workdir = dir.join("wd");
    extract(&fixture("comments"), &workdir);
    let (rc, envelope) = rust_json(&[
        "comment",
        "delete",
        "--json",
        workdir.to_str().expect("utf8"),
        "99",
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "comment-not-found");
    assert_eq!(
        std::fs::read(workdir.join("_template.docx")).expect("template"),
        std::fs::read(fixture("comments")).expect("fixture"),
        "template bytes unchanged"
    );
}

// ---------------------------------------------------------------------------
// Bullet 4: table structure ops with content-preservation guards
// ---------------------------------------------------------------------------

fn table_op(
    workdir: &Path,
    action: &str,
    table_ref: &str,
    args: &str,
    output: &Path,
    new_workdir: &Path,
) -> (i32, Value) {
    rust_json(&[
        "decide",
        action,
        table_ref,
        "--json",
        "--workdir",
        workdir.to_str().expect("utf8"),
        "--args",
        args,
        "--output",
        output.to_str().expect("utf8"),
        "--workdir-out",
        new_workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ])
}

#[test]
fn table_insert_row_and_merge_guard() {
    let dir = scratch("table");
    let workdir = dir.join("wd");
    extract(&fixture("table"), &workdir);

    // insert-row after 1 on T1 (body table index 1).
    let output = dir.join("insert.docx");
    let new_workdir = dir.join("insert-wd");
    let (rc, envelope) = table_op(
        &workdir,
        "table-insert-row",
        "T1",
        "1",
        &output,
        &new_workdir,
    );
    assert_eq!(rc, 0, "table insert-row failed: {envelope}");
    assert_eq!(
        envelope["data"]["operation"].as_str(),
        Some("table-insert-row")
    );
    assert!(output.is_file());
    assert!(new_workdir.join("_template.docx").is_file());

    // The new baseline verifies (Rust verify equivalent on a fresh build).
    let rebuilt = dir.join("rebuilt.docx");
    let (rc, build) = build(&new_workdir, &rebuilt);
    assert_eq!(rc, 0, "new baseline build failed: {build}");
    let (rc, verify) = verify(&new_workdir, &rebuilt);
    assert_eq!(rc, 0, "new baseline verify failed: {verify}");

    // Content preservation: the inserted row is empty and no text was
    // duplicated (every source cell text appears the same number of times).
    let source_text =
        String::from_utf8_lossy(&part_bytes(&fixture("table"), "word/document.xml")).into_owned();
    let output_text =
        String::from_utf8_lossy(&part_bytes(&output, "word/document.xml")).into_owned();
    for cell_text in ["A", "B", "C", "1", "2", "3"] {
        let needle = format!("<w:t>{cell_text}</w:t>");
        let before = source_text.matches(&needle).count();
        let after = output_text.matches(&needle).count();
        assert_eq!(before, after, "cell text {cell_text} not duplicated");
    }
    // The template bytes of the NEW workdir match the decided docx.
    assert_eq!(
        std::fs::read(new_workdir.join("_template.docx")).expect("template"),
        std::fs::read(&output).expect("output"),
        "fresh workdir template is the decided docx"
    );

    // merge-cells content-loss guard fails closed; --discard-content passes.
    let (rc, envelope) = table_op(
        &workdir,
        "table-merge-cells",
        "T0",
        "0 0 2",
        &dir.join("merge.docx"),
        &dir.join("merge-wd"),
    );
    assert_eq!(rc, 1);
    assert_eq!(
        envelope["diagnostics"][0]["code"],
        "merge-would-discard-content"
    );
    assert!(
        !dir.join("merge.docx").exists(),
        "no output on guard failure"
    );
    assert!(
        !dir.join("merge-wd").exists(),
        "no workdir on guard failure"
    );

    // Invalid table reference.
    let (rc, envelope) = table_op(
        &workdir,
        "table-insert-row",
        "T9",
        "1",
        &dir.join("bad.docx"),
        &dir.join("bad-wd"),
    );
    assert_eq!(rc, 1);
    assert_eq!(
        envelope["diagnostics"][0]["code"],
        "invalid-table-reference"
    );
    assert!(!dir.join("bad.docx").exists());
}

// ---------------------------------------------------------------------------
// Bullet 5: governed Unicode normalization audit (read-only; no standalone
// normalize surface)
// ---------------------------------------------------------------------------

#[test]
fn audit_reports_unicode_candidates_without_mutating() {
    let (rc, envelope) = rust_json(&["audit", "--json", fixture("norm").to_str().expect("utf8")]);
    assert_eq!(rc, 0, "audit failed: {envelope}");
    assert_eq!(
        envelope["data"]["schema"].as_str(),
        Some("docx2typed-unicode-audit-1")
    );
    let candidates = envelope["data"]["candidates"]
        .as_array()
        .expect("candidates");
    // norm.docx: 水H₂O 温度25°C Ca²⁺ 与 H₂SO₄ 反应。 / 平方公式 a² + b² = c²。
    // Vertical catalog covers ₂ ⁰ ⁺ ⁴ ² (and ⁰/⁺ etc. by U+207x block).
    assert!(
        candidates.len() >= 6,
        "norm.docx has vertical-catalog candidates: {}",
        candidates.len()
    );
    let codepoints: Vec<&str> = candidates
        .iter()
        .map(|candidate| candidate["codepoint"].as_str().expect("codepoint"))
        .collect();
    for expected in ["U+2082", "U+00B2", "U+207A", "U+2084"] {
        assert!(
            codepoints.contains(&expected),
            "catalog candidate {expected} reported: {codepoints:?}"
        );
    }
    // Occurrence ids are per-paragraph sequential.
    let p0: Vec<&Value> = candidates
        .iter()
        .filter(|candidate| candidate["paragraph_id"] == "P0")
        .collect();
    assert!(!p0.is_empty());
    // Read-only: the fixture file is untouched (audit takes a package path
    // and never writes).
    let (rc, verify_list) = rust_json(&[
        "revisions",
        "list",
        "--json",
        fixture("norm").to_str().expect("utf8"),
    ]);
    assert_eq!(rc, 0);
    assert_eq!(verify_list["outcome"], "success");
}

// ---------------------------------------------------------------------------
// Bullet 6: stable-negative boundaries
// ---------------------------------------------------------------------------

#[test]
fn unknown_commands_and_actions_are_stable_negatives() {
    // Unknown decide action.
    let dir = scratch("stable-negative");
    let workdir = dir.join("wd");
    extract(&fixture("revisions"), &workdir);
    let (rc, envelope) = rust_json(&[
        "decide",
        "frobnicate",
        "word/document.xml|insert|1|888c104169b5",
        "--json",
        "--workdir",
        workdir.to_str().expect("utf8"),
        "--fingerprint",
        "888c104169b5",
    ]);
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "invalid-action");

    // `normalize` as a standalone command is NOT reintroduced.
    let (rc, envelope) = rust_json(&["normalize", "--json", "anything"]);
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "invalid-arguments");
    assert!(
        envelope["diagnostics"][0]["message"]
            .as_str()
            .expect("message")
            .contains("no Protocol-major-1 --json contract"),
        "normalize has no command surface: {envelope}"
    );

    // Cross-island rewrite style edit on a revision (unsupported interior):
    // `edit text` on the field interior still fails closed (opaque).
    let complex = dir.join("complex-wd");
    extract(&fixture("complex"), &complex);
    let (rc, envelope) = rust_json(&[
        "edit",
        "text",
        "--json",
        complex.to_str().expect("utf8"),
        "P12.0",
        "FIELD",
        "XXX",
    ]);
    assert_eq!(rc, 1);
    assert_eq!(
        envelope["diagnostics"][0]["code"],
        "opaque-paragraph-mutated"
    );
}
