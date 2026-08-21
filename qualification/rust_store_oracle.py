"""Issue #57 differential oracle: runs the Python Reference store through
the same fault cuts / contention / idempotency / ENOSPC / corruption /
qualification scenarios the Rust binary exercises, and prints the frozen
outcome class (old / new / needs-recovery / diagnostic code) so
qualification/rust_store_gate.ps1 can compare both sides.

Subcommands (each prints one compact JSON line):
  cut W C            kill-at fault cut C during one mutation; recover;
                     print {"class": "old"|"new"|"needs-recovery"}
  mutate W [timeout_ms] [op] [scenario]
                     one mutation (no fault); scenario in
                     {plain, conflict, corrupt-pointer, enospc}; prints the
                     envelope or {"code": <frozen code>}
  replay W op        mutate twice with the same op-id; prints
                     {"replay_equal": bool, "generation_unchanged": bool}
  probe W            filesystem qualification; with
                     DOCX2TYPED_FORCE_UNQUALIFIED=1 prints the fail-closed
                     code instead
  hold W marker      acquire the Writer lane, write marker, sleep
"""
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.store import (  # noqa: E402
    GENERATION_CONFLICT,
    NEEDS_RECOVERY,
    RESERVE_DEPLETED,
    STORE_INVALID,
    UNSUPPORTED_BY_DESIGN,
    WRITER_BUSY,
    WRITER_TIMEOUT,
    Store,
    clear_faults,
    kill_at,
    set_fault,
)


def _plain_workdir(root: pathlib.Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "typed.md").write_text("hello\nworld\n", encoding="utf-8")
    (root / "edit.md").write_text("# draft\n", encoding="utf-8")
    (root / "format.json").write_text("{}", encoding="utf-8")
    (root / "styles.json").write_text("{}", encoding="utf-8")
    (root / "_template.docx").write_bytes(b"PK\x03\x04oracle-template")


def _ensure_store(root: pathlib.Path) -> Store:
    _plain_workdir(root)
    store = Store(root)
    if not store.store_dir.is_dir():
        Store.init(root, operation_id="0" * 32, input_sha256="oracle-input")
    return Store.open(root)


def _run(gen_dir, tx):
    (pathlib.Path(gen_dir) / "typed.md").write_text("hello\nworld\n", encoding="utf-8")
    (pathlib.Path(gen_dir) / "note.txt").write_text("note\n", encoding="utf-8")
    return (
        "success",
        {"changed": ["P0"]},
        "mutation",
        {"checks": [{"name": "mutated", "status": "pass"}]},
        [],
    )


def _mutate(store, op, *, timeout_ms=0, canonical="oracle-canonical"):
    pin = store.pin()
    try:
        envelope = store.mutate(
            operation="edit",
            operation_id=op,
            canonical=canonical,
            input_sha256=pin["manifest_sha256"],
            expected_generation=pin["generation"],
            run=_run,
            lock_timeout_ms=timeout_ms,
        )
        return {"outcome": envelope["outcome"]}
    except BaseException as exc:  # noqa: BLE001 - oracle classification
        code = getattr(exc, "code", None) or type(exc).__name__
        return {"code": code}


def cmd_cut(root: pathlib.Path, cut: str) -> None:
    store = _ensure_store(root)
    old_gen = store.pin()["generation"]
    op = "a" * 32
    kill_at(cut)
    try:
        pin = store.pin()
        try:
            store.mutate(
                operation="edit",
                operation_id=op,
                canonical="oracle-canonical",
                input_sha256=pin["manifest_sha256"],
                expected_generation=pin["generation"],
                run=_run,
            )
        except BaseException as exc:  # noqa: BLE001
            if type(exc).__name__ != "_Kill":
                clear_faults()
                print(json.dumps({"class": "unexpected", "error": repr(exc)}))
                return
    finally:
        clear_faults()
    recovered = Store.open(root).recover(auto=True)
    fresh = Store.open(root)
    if recovered["needs_recovery"]:
        result_class = "needs-recovery"
    elif fresh.pin()["generation"] == old_gen:
        result_class = "old"
    else:
        result_class = "new"
    print(json.dumps({"class": result_class, "cut": cut}))


def cmd_mutate(root: pathlib.Path, timeout_ms: int, op: str, scenario: str) -> None:
    store = _ensure_store(root)
    if scenario == "conflict":
        # First mutation commits; the second plans against the stale pin.
        first = store.pin()
        _mutate(store, op + "first", canonical="conflict-1")
        store2 = Store.open(root)
        try:
            store2.mutate(
                operation="edit",
                operation_id=op,
                canonical="conflict-2",
                input_sha256=first["manifest_sha256"],
                expected_generation=first["generation"],
                run=_run,
            )
            print(json.dumps({"outcome": "success"}))
        except BaseException as exc:  # noqa: BLE001
            print(json.dumps({"code": getattr(exc, "code", None) or type(exc).__name__}))
        return
    if scenario == "corrupt-pointer":
        (root / "workdir.json").write_text("{not json", encoding="utf-8")
        print(json.dumps(_mutate(store, op, canonical="corrupt-pointer")))
        return
    if scenario == "enospc":
        error = OSError("no space left")
        error.errno = 28
        set_fault("journal-write-prepared", error)
        try:
            result = _mutate(store, op)
        finally:
            clear_faults()
        result["reserve_released"] = (root / ".docx2typed-store" / "reserve").stat().st_size < 1024 * 1024
        result["marker"] = (root / ".docx2typed-store" / "reserve-depleted.json").is_file()
        print(json.dumps(result))
        return
    # plain: optional lane contention (holder process owns the lane).
    print(json.dumps(_mutate(store, op, timeout_ms=timeout_ms)))


def cmd_replay(root: pathlib.Path, op: str) -> None:
    store = _ensure_store(root)
    first = _mutate(store, op, canonical="replay-canonical")
    gen1 = store.pin()["generation"]
    second = _mutate(store, op, canonical="replay-canonical")
    print(
        json.dumps(
            {
                "replay_equal": first == second,
                "generation_unchanged": store.pin()["generation"] == gen1,
                "first": first,
            }
        )
    )


def cmd_probe(root: pathlib.Path) -> None:
    if os.environ.get("DOCX2TYPED_FORCE_UNQUALIFIED") == "1":
        try:
            _ensure_store(root)
            print(json.dumps({"qualified": True}))
        except BaseException as exc:  # noqa: BLE001 - fail-closed classification
            print(json.dumps({"code": getattr(exc, "code", None) or UNSUPPORTED_BY_DESIGN}))
        return
    _ensure_store(root)
    probe = json.loads((root / ".docx2typed-store" / "probe.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "qualified": bool(probe.get("qualified")),
                "checks": probe.get("checks", {}),
                "state_qualified": True,
            }
        )
    )


def cmd_corrupt_journal(root: pathlib.Path) -> None:
    """Crash mid-mutation (kill before prepared), tamper the intent record so
    the chain breaks, then recover: startup recovery must mark the
    transaction needs-recovery, never guess into repair."""
    store = _ensure_store(root)
    op = "c" * 32
    kill_at("journal-write-prepared")
    try:
        try:
            _mutate(store, op)
        except BaseException as exc:  # noqa: BLE001
            if type(exc).__name__ != "_Kill":
                raise
    finally:
        clear_faults()
    tx_dir = root / ".docx2typed-store" / "transactions" / op
    (tx_dir / "intent.json").write_text(
        '{"schema":"docx2typed-transaction-journal-1","phase":"intent","tampered":true}\n',
        encoding="utf-8",
    )
    recovered = Store.open(root).recover(auto=True)
    print(
        json.dumps(
            {
                "needs_recovery": bool(recovered["needs_recovery"]),
                "reason": (recovered["needs_recovery"] or [{}])[0].get("reason"),
            }
        )
    )


def cmd_hold(root: pathlib.Path, marker: pathlib.Path) -> None:
    store = _ensure_store(root)
    with store.writer():
        marker.write_text("ready", encoding="utf-8")
        time.sleep(120)


def main() -> None:
    command = sys.argv[1]
    if command == "cut":
        cmd_cut(pathlib.Path(sys.argv[2]), sys.argv[3])
    elif command == "mutate":
        root = pathlib.Path(sys.argv[2])
        timeout_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        op = sys.argv[4] if len(sys.argv) > 4 else "b" * 32
        scenario = sys.argv[5] if len(sys.argv) > 5 else "plain"
        cmd_mutate(root, timeout_ms, op, scenario)
    elif command == "replay":
        cmd_replay(pathlib.Path(sys.argv[2]), sys.argv[3])
    elif command == "probe":
        cmd_probe(pathlib.Path(sys.argv[2]))
    elif command == "corrupt-journal":
        cmd_corrupt_journal(pathlib.Path(sys.argv[2]))
    elif command == "hold":
        cmd_hold(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
    else:
        sys.exit(f"unknown oracle command: {command}")


if __name__ == "__main__":
    main()
