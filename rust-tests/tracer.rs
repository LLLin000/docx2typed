//! Issue #57 binary-level tracer: drives the installed-style `docx2typed`
//! binary through the real mutation (`edit`), store-backed external build
//! publication, store-state inspection, Writer lane contention, and REAL
//! process kill of the mutation (with recovery at the next entry point) —
//! the acceptance evidence the store unit tests cannot produce alone.

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::time::{Duration, Instant};

use serde_json::Value;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_docx2typed")
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn fixture() -> PathBuf {
    repo_root().join("corpus/release/plain.docx")
}

fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("docx2typed-tracer-{tag}-{}", std::process::id()));
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

/// Run a Python Reference command with cwd = repo root.
fn python(args: &[&str]) -> Output {
    Command::new("python")
        .args(args)
        .current_dir(repo_root())
        .output()
        .expect("python runs")
}

/// `python -m scripts --json extract <fixture> -o <outdir>` (JSON mode
/// store-births the workdir: generation 0, mirroring the Python reference
/// tests' `_extract` helper; human mode never births).
fn python_extract(outdir: &Path) {
    let output = python(&[
        "-m",
        "scripts",
        "--json",
        "extract",
        fixture().to_str().expect("utf8"),
        "-o",
        outdir.to_str().expect("utf8"),
    ]);
    assert!(
        output.status.success(),
        "python extract failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn generations(workdir: &Path) -> usize {
    let dir = workdir.join(".docx2typed-store/generations");
    std::fs::read_dir(&dir)
        .map(|entries| entries.count())
        .unwrap_or(0)
}

fn store_state(workdir: &Path) -> Value {
    let (rc, envelope) = rust_json(&["store-state", "--json", workdir.to_str().expect("utf8")]);
    assert_eq!(rc, 0, "store-state must exit 0");
    envelope.get("data").expect("data payload").clone()
}

fn op_id() -> String {
    docx2typed_protocol::new_operation_id()
}

/// Plain pre-store workdir (no store dir yet) — the lazy-upgrade path.
fn make_plain_workdir(root: &Path) {
    std::fs::create_dir_all(root).unwrap();
    std::fs::write(root.join("typed.md"), "hello\nworld\n").unwrap();
    std::fs::write(root.join("edit.md"), "# draft\n").unwrap();
    std::fs::write(root.join("format.json"), "{}").unwrap();
    std::fs::write(root.join("styles.json"), "{}").unwrap();
    std::fs::write(
        root.join("_template.docx"),
        std::fs::read(fixture()).unwrap(),
    )
    .unwrap();
}

// ---------------------------------------------------------------------------
// Real mutation path (`edit`) over real workdirs
// ---------------------------------------------------------------------------

#[test]
fn cli_edit_births_store_and_commits_generation() {
    let dir = scratch("edit-birth");
    let workdir = dir.join("wd");
    make_plain_workdir(&workdir);
    assert!(!workdir.join(".docx2typed-store").exists());
    let (rc, envelope) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc, 0, "edit must exit 0: {envelope}");
    assert_eq!(envelope["outcome"], "success");
    // The store was born (generation 0) and the ingress commit advanced it.
    assert!(workdir.join("workdir.json").is_file());
    assert_eq!(generations(&workdir), 2);
    let state = store_state(&workdir);
    assert_eq!(state["backed"], true);
    assert_eq!(state["filesystem_qualified"], true);
    assert_eq!(state["pending_recovery"], Value::Array(vec![]));
    // Durability ordering: pointer + manifest + evidence + ledger durable.
    let pointer: Value =
        serde_json::from_str(&std::fs::read_to_string(workdir.join("workdir.json")).unwrap())
            .unwrap();
    let newest = workdir
        .join(".docx2typed-store/generations")
        .join(pointer["generation"].as_str().unwrap());
    assert!(newest.join("generation.json").is_file());
    assert!(newest.join("run.evidence.json").is_file());
    assert!(newest.join("operation-ledger.json").is_file());
    // Root is the materialized mirror.
    assert_eq!(
        std::fs::read_to_string(workdir.join("typed.md")).unwrap(),
        "hello\nworld\n"
    );
}

#[test]
fn cli_edit_replay_returns_original_envelope() {
    let dir = scratch("edit-replay");
    let workdir = dir.join("wd");
    python_extract(&workdir);
    let op = op_id();
    let (rc1, first) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op,
    ]);
    assert_eq!(rc1, 0);
    let gens_after_first = generations(&workdir);
    let (rc2, second) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op,
    ]);
    assert_eq!(rc2, 0);
    assert_eq!(
        first, second,
        "identical retry must replay the original envelope"
    );
    // No duplicate effect: exactly one committed generation after the sync.
    assert_eq!(generations(&workdir), gens_after_first);
}

#[test]
fn cli_store_state_exposes_transaction_phases() {
    let dir = scratch("state");
    let workdir = dir.join("wd");
    make_plain_workdir(&workdir);
    let (rc, _) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc, 0);
    let state = store_state(&workdir);
    assert_eq!(state["backed"], true);
    assert_eq!(state["reserve_depleted"], false);
    assert!(state["generation"].is_string());
    assert!(state["pending_transactions"] == Value::Array(vec![]));
}

// ---------------------------------------------------------------------------
// External build publication through the Store (two-phase)
// ---------------------------------------------------------------------------

#[test]
fn cli_build_store_backed_two_phase_publication() {
    let dir = scratch("build-store");
    let workdir = dir.join("wd");
    python_extract(&workdir);
    let output = dir.join("built.docx");
    let op = op_id();
    let (rc, envelope) = rust_json(&[
        "build",
        "--json",
        workdir.to_str().expect("utf8"),
        "-o",
        output.to_str().expect("utf8"),
        "--operation-id",
        &op,
    ]);
    assert_eq!(rc, 0, "store-backed build failed: {envelope}");
    assert_eq!(envelope["outcome"], "success");
    // Output published byte-exact (no-op contract) with evidence + ledger
    // durable beside it.
    assert!(output.is_file());
    assert_eq!(
        std::fs::read(&output).unwrap(),
        std::fs::read(fixture()).unwrap()
    );
    assert!(PathBuf::from(format!("{}.evidence.json", output.display())).is_file());
    assert!(PathBuf::from(format!("{}.operation-ledger.json", output.display())).is_file());
    // The pointer never moved (external-only publication).
    assert_eq!(generations(&workdir), 1);
    // Identical retry replays the original envelope.
    let (rc2, replay) = rust_json(&[
        "build",
        "--json",
        workdir.to_str().expect("utf8"),
        "-o",
        output.to_str().expect("utf8"),
        "--operation-id",
        &op,
    ]);
    assert_eq!(rc2, 0);
    assert_eq!(replay, envelope);
}

// ---------------------------------------------------------------------------
// REAL process kill of the mutation; recovery at the next entry point
// ---------------------------------------------------------------------------

/// Spawn the binary with a kill fault that parks at the cut point (marker
/// file + sleep) so the test can kill the process for real.
fn spawn_killed_edit(workdir: &Path, cut: &str, marker: &Path) -> Child {
    let child = Command::new(bin())
        .args([
            "edit",
            "--json",
            workdir.to_str().expect("utf8"),
            "--operation-id",
            &op_id(),
        ])
        .env("DOCX2TYPED_FAULT", format!("kill:{cut}"))
        .env("DOCX2TYPED_FAULT_MARKER", marker)
        .env("DOCX2TYPED_FAULT_SLEEP_MS", "60000")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn killed edit");
    let deadline = Instant::now() + Duration::from_secs(30);
    while !marker.exists() {
        assert!(
            Instant::now() < deadline,
            "fault cut {cut} never reached its marker"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    child
}

#[test]
fn real_process_kill_before_pointer_recovers_old_at_next_entry() {
    let dir = scratch("kill-old");
    let workdir = dir.join("wd");
    make_plain_workdir(&workdir);
    let (rc, _) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc, 0, "initial birth+commit");
    let gens_before = generations(&workdir);
    let marker = dir.join("marker");
    let mut child = spawn_killed_edit(&workdir, "journal-write-prepared", &marker);
    // The process is parked mid-mutation: the journal is visible.
    let state = store_state(&workdir);
    let pending = state["pending_transactions"].as_array().expect("pending");
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0]["state"], "incomplete");
    let phases: Vec<&str> = pending[0]["phases"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|phase| phase.as_str())
        .collect();
    assert_eq!(phases, vec!["intent"]);
    // Real kill: the OS advisory lock and the journal are left as-is.
    child.kill().expect("kill");
    let _ = child.wait();
    // Next mutation entry runs startup recovery first (rolls back), then
    // commits: pointer never moved, so the old generation survives.
    let (rc2, envelope) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc2, 0, "entry recovery must succeed: {envelope}");
    assert_eq!(
        std::fs::read_to_string(workdir.join("typed.md")).unwrap(),
        "hello\nworld\n"
    );
    // The killed transaction was rolled back and its staged generation
    // pruned by GC (Python-identical: only the pointer-referenced generation
    // survives); the fresh commit adds exactly one generation, so the net
    // count is unchanged.
    assert_eq!(generations(&workdir), gens_before);
    assert!(store_state(&workdir)["pending_transactions"] == Value::Array(vec![]));
}

#[test]
fn real_process_kill_after_pointer_commits_recovers_forward() {
    let dir = scratch("kill-new");
    let workdir = dir.join("wd");
    make_plain_workdir(&workdir);
    let (rc, _) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc, 0, "initial birth+commit");
    let gens_before = generations(&workdir);
    let marker = dir.join("marker");
    let mut child = spawn_killed_edit(&workdir, "journal-write-completed", &marker);
    // Pointer committed, but the completed journal never landed.
    let state = store_state(&workdir);
    let pending = state["pending_transactions"].as_array().expect("pending");
    assert_eq!(pending.len(), 1);
    let phases: Vec<&str> = pending[0]["phases"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|phase| phase.as_str())
        .collect();
    assert_eq!(phases, vec!["intent", "prepared", "generation-committed"]);
    child.kill().expect("kill");
    let _ = child.wait();
    // Next mutation entry rolls the interrupted commit forward, then commits.
    let (rc2, envelope) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc2, 0, "roll-forward entry must succeed: {envelope}");
    let final_state = store_state(&workdir);
    assert!(final_state["pending_transactions"] == Value::Array(vec![]));
    // The interrupted generation was rolled forward (completed, not lost)
    // and the fresh commit landed; orphaned generations were pruned by GC,
    // so the net count is unchanged.
    assert_eq!(generations(&workdir), gens_before);
}

// ---------------------------------------------------------------------------
// Writer lane at the binary level (frozen diagnostics)
// ---------------------------------------------------------------------------

/// Python subprocess that holds the Writer lane, signaling readiness.
fn hold_writer(workdir: &Path, marker: &Path) -> Child {
    let root = repo_root().to_str().unwrap().replace('\\', "/");
    let wd = workdir.to_str().unwrap().replace('\\', "/");
    let marker = marker.to_str().unwrap().replace('\\', "/");
    let script = format!(
        "import sys, time; sys.path.insert(0, '{root}');\n\
         from scripts.store import Store;\n\
         s = Store('{wd}');\n\
         with s.writer():\n\
         \x20   import pathlib; pathlib.Path('{marker}').write_text('ready')\n\
         \x20   time.sleep(120)\n",
        root = root,
        wd = wd,
        marker = marker,
    );
    Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn writer holder")
}

fn wait_marker(marker: &Path) {
    let deadline = Instant::now() + Duration::from_secs(30);
    while !marker.exists() {
        assert!(Instant::now() < deadline, "lock holder never became ready");
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn cli_writer_busy_and_timeout_use_frozen_codes() {
    let dir = scratch("lane");
    let workdir = dir.join("wd");
    python_extract(&workdir);
    let marker = dir.join("holder-ready");
    let mut holder = hold_writer(&workdir, &marker);
    wait_marker(&marker);
    // Immediate contention -> writer-busy.
    let (rc, envelope) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc, 1);
    assert_eq!(envelope["diagnostics"][0]["code"], "writer-busy");
    // Bounded wait that expires -> writer-timeout.
    let started = Instant::now();
    let (rc2, envelope2) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
        "--lock-timeout-ms",
        "300",
    ]);
    assert_eq!(rc2, 1);
    assert_eq!(envelope2["diagnostics"][0]["code"], "writer-timeout");
    assert!(started.elapsed().as_secs() < 10);
    // Holder death (real kill) releases the lane.
    holder.kill().expect("kill holder");
    let _ = holder.wait();
    let (rc3, envelope3) = rust_json(&[
        "edit",
        "--json",
        workdir.to_str().expect("utf8"),
        "--operation-id",
        &op_id(),
    ]);
    assert_eq!(rc3, 0, "lane must be free after holder death: {envelope3}");
    assert_eq!(envelope3["outcome"], "success");
}
