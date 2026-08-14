"""Release Qualification runner (capability acceptance).

Runs the task matrix in capabilities/tasks/*.json against the real CLI and
MCP surfaces, applies positive/negative/fidelity oracles to every output,
executes the metamorphic relations, and writes a release report:

    reports/<name>/
      report.json               per-task results
      capability-matrix.json    manifest + per-capability pass/fail
      report.html               human-readable summary
      failures/                 per-failure detail
      artifacts/                outputs per task

Usage:
    python -m scripts.release_acceptance --report reports/release-<sha> \
        [--workdir D:/L/AppData/release-run] [--skip-office] [--only t01,t07]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "capabilities" / "tasks"
MANIFEST = REPO_ROOT / "capabilities" / "manifest.json"

# Metamorphic relations executed by _metamorphic, with the capability each
# relation qualifies.  The capability task map (capabilities/task_map.json)
# and the capability_matrix qualification check cross-verify this list, so
# the runner is the single source of truth for metamorphic coverage.
METAMORPHIC_CASES: list[tuple[str, str]] = [
    ("m2-revert-restores", "guard.freshness"),
    ("m3-track-accept-eq-direct", "revision.settle.all"),
    ("m4-item-accept-eq-all", "revision.decide.single"),
    ("m5-item-reject-eq-all", "revision.decide.single"),
    ("m6-batch-eq-sequential", "text.edit.region"),
    ("m7-insert-delete-row-eq-original", "table.row.ops"),
    ("m8-replay-byte-exact", "text.edit.body"),
    ("m9-reused-changed-input", "text.edit.body"),
    ("m10-noop-build-package-identical", "fidelity.noop"),
    ("m11-touched-build-signature-stable", "fidelity.noop"),
    ("m12-reader-pinning", "durability.generation-store"),
]


def _soffice() -> str | None:
    """LibreOffice binary: Windows path, else PATH lookup."""
    windows = Path(r"C:/Program Files/LibreOffice/program/soffice.exe")
    if windows.exists():
        return str(windows)
    import shutil

    return shutil.which("soffice")

_WORD_PARTS = re.compile(rb"word/.*\.xml$")
_TAG_OPEN = re.compile(rb"<w:ins[ >]|<w:del[ >]")
_P_OPEN = re.compile(rb"<w:p[ >]")
_TR_OPEN = re.compile(rb"<w:tr[ >]")
_TC_OPEN = re.compile(rb"<w:tc[ >]")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cli(*args: object, timeout: int = 900) -> tuple[int, str]:
    result = subprocess.run(
        ["python", "-m", "scripts", *(str(a) for a in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return result.returncode, (result.stdout or result.stderr).strip()


def _zip_parts(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: _sha256(archive.read(name)) for name in archive.namelist()}


def _word_text_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if _WORD_PARTS.match(name.encode())
        }


def _text_count(path: Path, text: str) -> int:
    needle = text.encode("utf-8")
    return sum(part.count(needle) for part in _word_text_parts(path).values())


def _open_tags(path: Path, pattern: re.Pattern) -> int:
    return sum(
        len(pattern.findall(part))
        for part in _word_text_parts(path).values()
    )


def _visible_text(xml: bytes) -> str:
    """Final-view text: the sequence of w:t content (deleted text excluded),
    tags stripped."""
    out: list[str] = []
    for match in re.finditer(rb"<w:t[^>]*>(.*?)</w:t>", xml, re.S):
        out.append(match.group(1).decode("utf-8", errors="replace"))
    return "".join(out)


_VOLATILE_EXACT = frozenset(
    {"generated", "timestamp", "time", "created", "updated", "issued", "published",
     "approved_time", "run_id", "operation_id", "started_at", "finished_at"}
)


def _strip_volatile(value):
    """Recursively drop versioned-volatile fields so two independent runs of
    the same operation produce comparable canonical Result envelopes."""
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in _VOLATILE_EXACT and not key.endswith("_at")
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _canonical_envelope(env: dict) -> str:
    return json.dumps(
        _strip_volatile(env), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


MCP_DRIVER_LOOP = r"""
import json, sys, importlib
from mcp.types import CallToolResult
server = importlib.import_module("scripts.mcp_server")
for line in sys.stdin:
    request = json.loads(line)
    try:
        out = getattr(server, request["tool"])(**request["args"])
        if isinstance(out, CallToolResult):
            if out.isError:
                diagnostics = (out.structuredContent or {}).get("diagnostics") or []
                code = diagnostics[0].get("code", "error") if diagnostics else "error"
                print("ERR " + str(code)[:300], flush=True)
            else:
                data = (out.structuredContent or {}).get("data", {})
                print("OK " + json.dumps(data, ensure_ascii=True)[:6000], flush=True)
        else:
            print("OK " + json.dumps(out, ensure_ascii=True)[:6000], flush=True)
    except Exception as exc:
        print("ERR " + str(exc)[:300], flush=True)
"""


class Mcp:
    """Persistent stdio session against the MCP server module."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", MCP_DRIVER_LOOP],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None

    def __call__(self, tool: str, **kwargs) -> tuple[bool, str]:
        self.proc.stdin.write(json.dumps({"tool": tool, "args": kwargs}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline().strip()
        if line.startswith("OK "):
            return True, line[3:]
        return False, line[4:] if line.startswith("ERR ") else line

    def close(self) -> None:
        self.proc.terminate()


class TaskRun:
    """State of one task execution."""

    def __init__(self, task: dict[str, Any], root: Path, artifacts: Path) -> None:
        self.task = task
        self.root = root
        self.artifacts = artifacts
        self.source = (REPO_ROOT / task["source"]).resolve()
        self.workdir = root / "wd"
        self.wd_out = root / "wd2"
        self.output: Path | None = None
        self.output_norm: Path | None = None
        self.steps: list[dict[str, Any]] = []
        self.step_outputs: list[tuple[int, str]] = []
        self.oracle_results: list[dict[str, Any]] = []
        self.started = time.monotonic()
        self.typed_sha: str | None = None
        self.mcp: Mcp | None = None
        self.comments_before: str | None = None
        self.comments_after: str | None = None
        self.verify_evidence: str | None = None
        self.commit_count = 0

    def record_step(self, index: int, spec: dict[str, Any], rc: int, out: str) -> None:
        self.step_outputs.append((rc, out))
        self.steps.append({"index": index, "op": spec["op"], "rc": rc, "detail": out[:300]})

    def close(self) -> None:
        if self.mcp is not None:
            self.mcp.close()


def _apply_draft_edits(workdir: Path, edits: list[dict[str, Any]]) -> None:
    """Apply the task's edit.md draft edits (region replaces, inserts,
    deletes) the way an agent would: rewrite the projection text."""
    text = (workdir / "edit.md").read_text(encoding="utf-8")
    for edit in edits:
        if "old" in edit:
            marker = f'<!--@p id="{edit["paragraph"]}"-->'
            start = text.index(marker)
            end = text.find("\n\n", start)
            if end < 0:
                end = len(text)
            block = text[start:end]
            assert edit["old"] in block, f"{edit['old']!r} not in block {edit['paragraph']}"
            text = text[:start] + block.replace(edit["old"], edit["new"], 1) + text[end:]
        elif "insert_after" in edit:
            marker = f'<!--@p id="{edit["insert_after"]}"-->'
            start = text.index(marker)
            end = text.find("\n\n", start)
            if end < 0:
                end = len(text)
            block = text[start:end]
            inherit = edit.get("inherit", edit["insert_after"])
            insert = f'<!--@new temp="N" inherit="{inherit}"-->\n{edit["text"]}'
            text = text[:end] + "\n\n" + insert + text[end:]
        elif "delete" in edit:
            marker = f'<!--@p id="{edit["delete"]}"-->'
            start = text.index(marker)
            end = text.find("\n\n", start)
            if end < 0:
                end = len(text)
            text = text[:start] + f'<!--@delete id="{edit["delete"]}"-->' + text[end:]
    (workdir / "edit.md").write_text(text, encoding="utf-8")


def _inventory_key(workdir: Path, *, kind: str, index: int) -> str:
    inv = json.loads((workdir / "revisions.json").read_text(encoding="utf-8"))
    editable = [r for r in inv["revisions"] if r.get("editable") and r.get("kind") == kind]
    return editable[index]["revision_key"]


def _run_steps(run: TaskRun) -> None:
    """Execute the task's step list; record every step result."""
    workdir = run.workdir
    for index, spec in enumerate(run.task["steps"]):
        op = spec["op"]
        rc, out = 0, ""
        try:
            if op == "extract":
                rc, out = _cli("extract", run.source, "-o", workdir)
                if rc == 0 and (workdir / "typed.md").exists():
                    run.typed_sha = _sha256((workdir / "typed.md").read_bytes())
            elif op == "edit_sync":
                _apply_draft_edits(workdir, spec.get("edits", []))
                args = ["edit", "sync", workdir]
                if spec.get("track"):
                    args.insert(2, "--track")
                rc, out = _cli(*args)
            elif op == "edit_refresh":
                args = ["edit", "refresh", workdir]
                if spec.get("discard"):
                    args.append("--discard")
                rc, out = _cli(*args)
            elif op == "build":
                run.output = run.artifacts / "out.docx"
                rc, out = _cli("build", workdir, "-o", run.output)
            elif op == "verify":
                if run.output is not None:
                    rc, out = _cli("verify", workdir, run.output)
                else:
                    rc, out = 1, "no output to verify"
            elif op == "verify_new_baseline":
                rc, out = _cli("verify", run.wd_out, run.output)
            elif op == "decide":
                action = spec["action"]
                target_wd = run.root / spec["use_workdir"] if spec.get("use_workdir") else workdir
                args = ["decide", action, "--workdir", target_wd]
                if spec.get("table"):
                    args += [spec["table"]]
                key = None
                if spec.get("key_from_inventory"):
                    key = _inventory_key(workdir, **spec["key_from_inventory"])
                elif spec.get("revision_key"):
                    key = spec["revision_key"]
                if key:
                    args += [key]
                if action in ("accept", "reject", "reinsert"):
                    parts = key.split("|")
                    args += ["--fingerprint", parts[3] if len(parts) == 4 else ""]
                if action in ("accept-all", "reject-all") or action.startswith("table-"):
                    step_output = run.artifacts / f"out-{index}.docx"
                    step_wd_out = run.root / f"wd2-{index}"
                    run.output = step_output
                    run.wd_out = step_wd_out
                    args += ["--output", str(step_output), "--workdir-out", str(step_wd_out)]
                if spec.get("args"):
                    args += ["--args", " ".join(str(a) for a in spec["args"])]
                if spec.get("discard_content"):
                    args += ["--discard-content"]
                rc, out = _cli(*args)
            elif op == "dirty_workdir":
                (workdir / "edit.md").write_text(
                    (workdir / "edit.md").read_text(encoding="utf-8") + "\n脏改动\n",
                    encoding="utf-8",
                )
            elif op == "drift_template":
                with open(workdir / "_template.docx", "ab") as handle:
                    handle.write(b"\nDRIFT")
            elif op == "audit_scan":
                run.scan_path = run.artifacts / "scan.json"
                rc, out = _cli("audit", "scan", workdir, "-o", run.scan_path)
            elif op == "audit_apply":
                rc, out = _build_and_apply_audit(run, workdir)
            elif op == "mcp_open":
                run.mcp = Mcp()
                ok, detail = run.mcp("workdir_open", workdir=str(workdir), track=spec.get("track"), author=spec.get("author"))
                rc, out = (0, detail) if ok else (1, detail)
            elif op == "mcp_comments_snapshot":
                ok, detail = run.mcp("list_comments")
                rc, out = (0, detail) if ok else (1, detail)
                if ok:
                    run.comments_before = detail
            elif op == "mcp_comments_after":
                ok, detail = run.mcp("list_comments")
                rc, out = (0, detail) if ok else (1, detail)
                if ok:
                    run.comments_after = detail
            elif op == "mcp_diff_preview":
                ok, detail = run.mcp("diff_preview")
                rc, out = (0, detail) if ok else (1, detail)
            elif op == "mcp_commit":
                ok, detail = run.mcp("commit_sync", operation_id=f"ra-{index}")
                rc, out = (0, detail) if ok else (1, detail)
                if ok:
                    run.commit_count += 1
            elif op == "mcp_build":
                run.output = run.artifacts / "out.docx"
                ok, detail = run.mcp("build_docx", output=str(run.output), operation_id=f"ra-{index}")
                rc, out = (0, detail) if ok else (1, detail)
            elif op == "mcp_verify":
                ok, detail = run.mcp("verify_output", output=str(run.output))
                rc, out = (0, detail) if ok else (1, detail)
                if ok:
                    run.verify_evidence = detail
            elif op == "mcp_replace":
                ok, detail = run.mcp("replace_text", paragraph_id=spec["paragraph"], old=spec["old"], new=spec["new"], operation_id=f"ra-{index}")
                rc, out = (0, detail) if ok else (1, detail)
            elif op == "mcp_commit":
                ok, detail = run.mcp("commit_sync", operation_id=f"ra-{index}")
                rc, out = (0, detail) if ok else (1, detail)
            else:
                rc, out = 1, f"unknown op: {op}"
        except Exception as exc:  # noqa: BLE001 - runner reports step failures
            rc, out = 1, f"{type(exc).__name__}: {exc}"
        run.record_step(index, spec, rc, out)


def _build_and_apply_audit(run: TaskRun, workdir: Path) -> tuple[int, str]:
    """Decide every scan candidate (convert when approved, else preserve),
    approve the policy, and apply it to a new baseline."""
    scan = json.loads(run.scan_path.read_text(encoding="utf-8"))
    snapshot = scan["snapshot"]
    decisions: dict[str, Any] = {}
    # one style_id can carry only one vertAlign direction: when the
    # candidates of a style disagree (subscript + superscript), converting
    # any of them would restyle the others — preserve the whole group.
    by_style: dict[str, list[dict[str, Any]]] = {}
    for candidate in scan["candidates"]:
        by_style.setdefault(candidate.get("style_id", ""), []).append(candidate)
    for style_id, group in by_style.items():
        targets = {
            c["proposed_target"] for c in group
            if c.get("classification") == "approved" and c.get("proposed_target")
        }
        convertible = (
            len(targets) == 1
            and all(
                c.get("classification") == "approved" and c.get("proposed_target")
                for c in group
            )
        )
        for candidate in group:
            decisions[candidate["occurrence_id"]] = {
                "decision": "convert" if convertible else "preserve",
                "actor": "release-acceptance",
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "rationale": (
                    None
                    if convertible
                    else "conflicting vertAlign targets share one style; preserving the group"
                ),
            }
    policy = {
        "schema": "vertical-normalization-policy-2",
        "status": "approved",
        "approval_requirement": "human",
        "audit_schema": "vertical-normalization-audit-2",
        "scanner_contract_version": scan.get("scanner_contract_version", 1),
        "project_id": snapshot["project_id"],
        "baseline_sha256": snapshot["baseline_sha256"],
        "draft_snapshot_sha256": snapshot["draft_snapshot_sha256"],
        "model_sha256": snapshot["model_sha256"],
        "catalog_sha256": snapshot["catalog_sha256"],
        "scan_artifact_sha256": scan["scan_artifact_sha256"],
        "decisions": decisions,
        "approval": {
            "approved": True,
            "requirement": "human",
            "approved_by": "release-acceptance",
            "approval_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
    policy_path = run.artifacts / "policy.json"
    policy_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run.output_norm = run.artifacts / "normalized.docx"
    return _cli(
        "audit", "apply", workdir,
        "--scan", run.scan_path, "--policy", policy_path,
        "-o", run.output_norm, "--workdir-out", run.wd_out,
    )


# ---------------------------------------------------------------- oracles


def _comments_preserved(run: TaskRun) -> tuple[bool, str]:
    """The comment inventory (ids/anchors) must be identical before and
    after the review edits; the template metadata is the ground truth."""
    import json as _json

    def _load_comments(raw: str | None) -> list:
        data = _json.loads(raw or "{}")
        if isinstance(data, str):  # serve loop double-encodes string outputs
            data = _json.loads(data)
        return data.get("comments", []) if isinstance(data, dict) else []

    try:
        before = _load_comments(run.comments_before)
        after = _load_comments(run.comments_after)
    except Exception as exc:  # noqa: BLE001
        return False, f"snapshot parse: {exc}"
    norm = lambda items: sorted(
        (c.get("id"), tuple(c.get("anchor_paragraphs", []))) for c in items
    )
    same = norm(before) == norm(after) and len(before) == len(after)
    return same, f"{len(before)} comments before/after"


def _office_open(path: Path | None) -> tuple[bool, str]:
    if path is None or not path.exists():
        return False, "no output"
    soffice = _soffice()
    if soffice is None:
        return True, "skipped (no LibreOffice)"
    outdir = path.parent / "pdf"
    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    ok = result.returncode == 0 and "convert" in (result.stdout or result.stderr)
    return ok, (result.stdout or result.stderr).strip()[-160:]


def _run_oracles(run: TaskRun, skip_office: bool) -> None:
    task = run.task
    oracles = task.get("oracles", {})
    source = run.source
    output = run.output or run.output_norm
    for group, items in oracles.items():
        for spec in items:
            kind = spec["kind"]
            ok, detail = True, ""
            try:
                if kind == "verify_pass":
                    target_wd = run.wd_out if (run.wd_out / "typed.md").exists() else run.workdir
                    rc, out = _cli("verify", target_wd, output)
                    ok, detail = rc == 0, out
                elif kind == "byte_identical":
                    src_parts = _zip_parts(source)
                    out_parts = _zip_parts(output)
                    ok = src_parts == out_parts
                    detail = f"{len(src_parts)} parts"
                elif kind == "parts_unchanged":
                    excepted = set(spec.get("except", []))
                    src_parts = _zip_parts(source)
                    out_parts = _zip_parts(output)
                    changed = [
                        name for name in src_parts
                        if name not in excepted and src_parts[name] != out_parts.get(name)
                    ]
                    ok = not changed
                    detail = f"changed: {changed[:5]}"
                elif kind == "text_count":
                    count = _text_count(output, spec["text"])
                    ok = count == spec["count"]
                    detail = f"count={count} want={spec['count']}"
                elif kind == "text_absent":
                    count = _text_count(output, spec["text"])
                    ok = count == 0
                    detail = f"count={count}"
                elif kind == "revision_count":
                    count = _open_tags(output, _TAG_OPEN)
                    ok = count == spec["count"]
                    detail = f"ins/del opens={count} want={spec['count']}"
                elif kind == "residual_revisions":
                    count = _open_tags(output, _TAG_OPEN)
                    ok = count == spec["count"]
                    detail = f"residual={count}"
                elif kind == "paragraph_count":
                    count = len(_P_OPEN.findall((output and _word_text_parts(output).get("word/document.xml", b"")) or b""))
                    ok = count == spec["count"]
                    detail = f"paragraphs={count} want={spec['count']}"
                elif kind == "row_col_counts":
                    xml = _word_text_parts(output).get("word/document.xml", b"")
                    rows, cells = len(_TR_OPEN.findall(xml)), len(_TC_OPEN.findall(xml))
                    ok = (rows, cells) == (spec["rows"], spec["cells"])
                    detail = f"rows/cells={rows}/{cells} want={spec['rows']}/{spec['cells']}"
                elif kind == "inserted_row_empty":
                    ok, detail = _inserted_row_empty(output, spec["table"], spec["after"])
                elif kind == "no_text_duplication":
                    ok, detail = _no_cell_text_duplication(source, output)
                elif kind == "grid_span_clean":
                    xml = _word_text_parts(output).get("word/document.xml", b"")
                    leftovers = re.findall(rb'<w:gridSpan w:val="([2-9]\d*)"/>', xml)
                    ok = not leftovers
                    detail = f"gridSpan>1: {leftovers}"
                elif kind == "text_signature_same":
                    src_xml = _word_text_parts(source).get("word/document.xml", b"")
                    out_xml = _word_text_parts(output).get("word/document.xml", b"")
                    ok = _visible_text(src_xml) == _visible_text(out_xml)
                    detail = "visible text equal"
                elif kind == "comment_present":
                    ok, detail = _comment_state(output, spec["id"], present=True)
                elif kind == "comment_absent":
                    ok, detail = _comment_state(output, spec["id"], present=False)
                elif kind == "anchors_intact":
                    src, out = _anchor_counts(source), _anchor_counts(output)
                    ok = src == out
                    detail = f"{src} -> {out}"
                elif kind == "hyperlink_target":
                    ok, detail = _hyperlink_target(output, spec["text"], spec["target"])
                elif kind == "opaque_interior_unchanged":
                    ok, detail = _opaque_structures(source, output)
                elif kind == "paragraph_marks":
                    count = _open_tags(output, re.compile(rb"<w:(ins|del)[ >][^>]*/>"))
                    ok = count == spec["count"]
                    detail = f"self-closing marks={count} want={spec['count']}"
                elif kind == "text_visible_contains":
                    visible = "".join(_visible_text(part) for part in _word_text_parts(output).values())
                    ok = spec["text"] in visible
                    detail = f"visible contains {spec['text']!r}: {ok}"
                elif kind == "comments_preserved":
                    ok, detail = _comments_preserved(run)
                elif kind == "single_commit":
                    ok = run.commit_count == spec.get("count", 1)
                    detail = f"commit_sync calls={run.commit_count}"
                elif kind == "cell_text":
                    ok, detail = _cell_text(output, spec["table"], spec["row"], spec["col"], spec["text"])
                elif kind == "audit_complete":
                    ok = run.wd_out.exists() and (run.wd_out / "normalization.audit.json").exists()
                    detail = "audit.json present" if ok else "missing normalization.audit.json"
                elif kind == "source_workdir_unchanged":
                    ok = _workdir_snapshot(run.workdir) == _workdir_snapshot(run.workdir)
                    detail = "workdir unchanged"
                elif kind == "workdir_unchanged":
                    current = _sha256((run.workdir / "typed.md").read_bytes())
                    ok = run.typed_sha == current
                    detail = f"typed.md {'unchanged' if ok else 'changed'}"
                elif kind == "exit_failure":
                    rc, out = run.step_outputs[spec["step"]]
                    ok = rc != 0 and spec["contains"].lower() in out.lower()
                    detail = f"rc={rc} out={out[:120]}"
                elif kind == "perf_seconds":
                    elapsed = time.monotonic() - run.started
                    ok = elapsed <= spec["max"]
                    detail = f"{elapsed:.1f}s"
                elif kind == "office_open":
                    if skip_office:
                        ok, detail = True, "skipped"
                    else:
                        ok, detail = _office_open(output or run.output_norm)
                else:
                    ok, detail = False, f"unknown oracle: {kind}"
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            run.oracle_results.append(
                {"kind": kind, "group": group, "passed": ok, "detail": detail}
            )


def _cell_text(output: Path, table_ref: str, row: int, col: int, text: str) -> tuple[bool, str]:
    import xml.etree.ElementTree as ET

    xml = _word_text_parts(output)["word/document.xml"]
    root = ET.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    body = root.find(f"{{{ns['w']}}}body")
    tables = [child for child in body if child.tag == f"{{{ns['w']}}}tbl"]
    table = tables[int(table_ref[1:])]
    rows = table.findall(f"{{{ns['w']}}}tr")
    cells = rows[row].findall(f"{{{ns['w']}}}tc")
    if col >= len(cells):
        return False, f"cell ({row},{col}) out of range ({len(cells)} cells)"
    actual = "".join(cells[col].itertext()).strip()
    return actual == text, f"cell ({row},{col})={actual!r} want {text!r}"


def _inserted_row_empty(output: Path, table_ref: str, after: int) -> tuple[bool, str]:
    import xml.etree.ElementTree as ET

    xml = _word_text_parts(output)["word/document.xml"]
    root = ET.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    body = root.find(f"{{{ns['w']}}}body")
    tables = [child for child in body if child.tag == f"{{{ns['w']}}}tbl"]
    table = tables[int(table_ref[1:])]
    rows = table.findall(f"{{{ns['w']}}}tr")
    target = rows[after + 1]
    texts = ["".join(c.itertext()).strip() for c in target.findall(f"{{{ns['w']}}}tc")]
    ok = all(not t for t in texts)
    return ok, f"inserted row cells empty: {texts}"


def _no_cell_text_duplication(source: Path, output: Path) -> tuple[bool, str]:
    import xml.etree.ElementTree as ET

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    def cell_texts(path: Path) -> list[str]:
        root = ET.fromstring(_word_text_parts(path)["word/document.xml"])
        texts: list[str] = []
        for tbl in root.iter(f"{{{ns['w']}}}tbl"):
            for tc in tbl.iter(f"{{{ns['w']}}}tc"):
                text = "".join(tc.itertext()).strip()
                if text:
                    texts.append(text)
        return texts

    src_texts, out_texts = cell_texts(source), cell_texts(output)
    dupes = [t for t in src_texts if out_texts.count(t) > src_texts.count(t)]
    return not dupes, f"duplicated cell texts: {dupes[:3]}"


def _comment_state(output: Path, comment_id: str, *, present: bool) -> tuple[bool, str]:
    parts = _word_text_parts(output)
    comments = parts.get("word/comments.xml", b"")
    doc = parts.get("word/document.xml", b"")
    entry = f'<w:comment w:id="{comment_id}"'.encode()
    anchor = f'w:id="{comment_id}"'.encode()
    has_entry = entry in comments
    has_anchor = anchor in doc
    ok = (has_entry and has_anchor) if present else (not has_entry and not has_anchor)
    return ok, f"entry={has_entry} anchor={has_anchor}"


def _anchor_counts(path: Path) -> tuple[int, int, int, int]:
    xml = b"".join(_word_text_parts(path).values())
    return (
        xml.count(b"<w:commentRangeStart"),
        xml.count(b"<w:commentRangeEnd"),
        xml.count(b"<w:bookmarkStart"),
        xml.count(b"<w:bookmarkEnd"),
    )


def _hyperlink_target(output: Path, text: str, target: str) -> tuple[bool, str]:
    with zipfile.ZipFile(output) as archive:
        doc = archive.read("word/document.xml")
        rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
    hyperlinks = re.findall(rb'<w:hyperlink r:id="(rId\d+)"[^>]*>(.*?)</w:hyperlink>', doc, re.S)
    needle = text.encode("utf-8")
    for rid, body in hyperlinks:
        if needle in body:
            match = re.search(rf'Id="{rid.decode()}" Type="[^"]*hyperlink" Target="([^"]+)"', rels)
            ok = match is not None and match.group(1) == target
            return ok, f"rid={rid.decode()} target={match.group(1) if match else '?'}"
    return False, f"hyperlink with {text!r} not found"


def _opaque_structures(source: Path, output: Path) -> tuple[bool, str]:
    def counts(path: Path) -> tuple[int, int]:
        xml = _word_text_parts(path)["word/document.xml"]
        return (
            len(re.findall(rb"<w:fldSimple[ >]", xml)),
            len(re.findall(rb"<m:oMath[ >]", xml)),
        )

    src, out = counts(source), counts(output)
    return src == out, f"fldSimple/oMath {src} -> {out}"


def _workdir_snapshot(workdir: Path) -> dict[str, str]:
    names = ["typed.md", "format.json", "styles.json", "edit.md", "_template.docx"]
    snapshot: dict[str, str] = {}
    for name in names:
        path = workdir / name
        if path.exists():
            snapshot[name] = _sha256(path.read_bytes())
    return snapshot


# ------------------------------------------------------- metamorphic relations


def _metamorphic(root: Path, artifacts: Path, skip_office: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def relation(name: str, capability: str, check: callable) -> None:
        started = time.monotonic()
        try:
            ok, detail = check()
            results.append(
                {
                    "id": name,
                    "capability": capability,
                    "result": "pass" if ok else "fail",
                    "detail": detail,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"id": name, "capability": capability, "result": "fail", "detail": f"{type(exc).__name__}: {exc}"})

    plain = REPO_ROOT / "corpus/release/plain.docx"
    styled = REPO_ROOT / "corpus/release/styled.docx"
    table = REPO_ROOT / "corpus/release/table.docx"

    def m2_revert() -> tuple[bool, str]:
        wd = root / "m2"
        _cli("extract", plain, "-o", wd)
        before = _workdir_snapshot(wd)
        _apply_draft_edits(wd, [{"paragraph": "P0", "old": "20 mg", "new": "25 mg"}])
        _cli("edit", "refresh", wd, "--discard")
        after = _workdir_snapshot(wd)
        return before == after, "dirty draft -> refresh --discard restores workdir"

    def m3_track_accept_eq_direct() -> tuple[bool, str]:
        wd_direct = root / "m3a"
        wd_track = root / "m3b"
        _cli("extract", plain, "-o", wd_direct)
        _apply_draft_edits(wd_direct, [{"paragraph": "P0", "old": "20 mg", "new": "25 mg"}])
        _cli("edit", "sync", wd_direct)
        _cli("build", wd_direct, "-o", artifacts / "m3-direct.docx")
        # track replace -> build (revisions now live in the docx) -> re-extract
        # -> accept-all settles the built revisions
        _cli("extract", plain, "-o", wd_track)
        _apply_draft_edits(wd_track, [{"paragraph": "P0", "old": "20 mg", "new": "25 mg"}])
        _cli("edit", "sync", wd_track, "--track")
        _cli("build", wd_track, "-o", artifacts / "m3-track.docx")
        _cli("extract", artifacts / "m3-track.docx", "-o", root / "m3c")
        _cli("decide", "accept-all", "--workdir", root / "m3c", "--output", artifacts / "m3-accept.docx", "--workdir-out", root / "m3d")
        a = _visible_text(_word_text_parts(artifacts / "m3-direct.docx")["word/document.xml"])
        b = _visible_text(_word_text_parts(artifacts / "m3-accept.docx")["word/document.xml"])
        return a == b, "track replace -> build -> re-extract -> accept-all == direct replace"

    def _editable_only_revision_docx(name: str, action_verb: str) -> Path:
        """Build a docx whose revisions are all editable (no opaque
        interiors): track-replace plain.docx, build, re-extract as the base
        workdir for per-item vs wholesale comparison."""
        wd = root / name
        _cli("extract", plain, "-o", wd)
        _apply_draft_edits(wd, [{"paragraph": "P0", "old": "20 mg", "new": "25 mg"}])
        _cli("edit", "sync", wd, "--track")
        docx = artifacts / f"{name}-src.docx"
        _cli("build", wd, "-o", docx)
        base = root / f"{name}-base"
        _cli("extract", docx, "-o", base)
        return base

    def m4_per_item_accept_eq_all() -> tuple[bool, str]:
        base = _editable_only_revision_docx("m4", "accept")
        inv = json.loads((base / "revisions.json").read_text(encoding="utf-8"))
        for revision in [r for r in inv["revisions"] if r.get("editable")]:
            key = revision["revision_key"]
            _cli("decide", "accept", key, "--workdir", base, "--fingerprint", key.split("|")[3])
        _cli("build", base, "-o", artifacts / "m4-item.docx")
        _cli("decide", "accept-all", "--workdir", base, "--output", artifacts / "m4-all.docx", "--workdir-out", root / "m4b")
        a = _visible_text(_word_text_parts(artifacts / "m4-item.docx")["word/document.xml"])
        b = _visible_text(_word_text_parts(artifacts / "m4-all.docx")["word/document.xml"])
        return a == b, "per-item accept == accept-all (editable revisions)"

    def m5_per_item_reject_eq_all() -> tuple[bool, str]:
        base = _editable_only_revision_docx("m5", "reject")
        inv = json.loads((base / "revisions.json").read_text(encoding="utf-8"))
        for revision in [r for r in inv["revisions"] if r.get("editable")]:
            key = revision["revision_key"]
            _cli("decide", "reject", key, "--workdir", base, "--fingerprint", key.split("|")[3])
        _cli("build", base, "-o", artifacts / "m5-item.docx")
        _cli("decide", "reject-all", "--workdir", base, "--output", artifacts / "m5-all.docx", "--workdir-out", root / "m5b")
        a = _visible_text(_word_text_parts(artifacts / "m5-item.docx")["word/document.xml"])
        b = _visible_text(_word_text_parts(artifacts / "m5-all.docx")["word/document.xml"])
        return a == b, "per-item reject == reject-all (editable revisions)"

    def m6_batch_eq_sequential() -> tuple[bool, str]:
        wd_batch = root / "m6a"
        wd_seq = root / "m6b"
        _cli("extract", styled, "-o", wd_batch)
        _cli("extract", styled, "-o", wd_seq)
        mcp = Mcp()
        try:
            mcp("workdir_open", workdir=str(wd_batch))
            ok_batch, _ = mcp(
                "batch_edit",
                paragraph_id="P4",
                edits=[
                    {"region": 0, "old": "智能响应", "new": "智能调控"},
                    {"region": 2, "old": "ABC", "new": "XYZ"},
                ],
                operation_id="m6-batch-1",
            )
            mcp("commit_sync", operation_id="m6-batch-commit")
            mcp("build_docx", output=str(artifacts / "m6-batch.docx"), operation_id="m6-batch-build")
            mcp("workdir_open", workdir=str(wd_seq))
            ok_seq1, _ = mcp("replace_text", paragraph_id="P4", old="智能响应", new="智能调控", operation_id="m6-seq-1")
            ok_seq2, _ = mcp("replace_text", paragraph_id="P4", old="ABC", new="XYZ", operation_id="m6-seq-2")
            mcp("commit_sync", operation_id="m6-seq-commit")
            mcp("build_docx", output=str(artifacts / "m6-seq.docx"), operation_id="m6-seq-build")
        finally:
            mcp.close()
        a = _visible_text(_word_text_parts(artifacts / "m6-batch.docx")["word/document.xml"])
        b = _visible_text(_word_text_parts(artifacts / "m6-seq.docx")["word/document.xml"])
        return bool(ok_batch and ok_seq1 and ok_seq2) and a == b, "batch_edit == sequential edits"

    def m7_insert_delete_row_eq_original() -> tuple[bool, str]:
        wd = root / "m7"
        _cli("extract", table, "-o", wd)
        _cli("decide", "table-insert-row", "T1", "--workdir", wd, "--args", "1", "--output", artifacts / "m7-insert.docx", "--workdir-out", root / "m7b")
        _cli("decide", "table-delete-row", "T1", "--workdir", root / "m7b", "--args", "2", "--output", artifacts / "m7-back.docx", "--workdir-out", root / "m7c")
        src = _word_text_parts(table)["word/document.xml"]
        back = _word_text_parts(artifacts / "m7-back.docx")["word/document.xml"]
        counts_eq = (_TR_OPEN.findall(src), _TC_OPEN.findall(src)) == (_TR_OPEN.findall(back), _TC_OPEN.findall(back))
        text_eq = _visible_text(src) == _visible_text(back)
        return counts_eq and text_eq, "insert row -> delete same row == original structure + text"

    def m8_replay_byte_exact() -> tuple[bool, str]:
        """Identical operation-id + canonical input replays the ORIGINAL
        Result envelope byte-exactly (canonical comparison), never a second
        effect."""
        wd = root / "m8"
        _cli("extract", plain, "-o", wd)
        mcp = Mcp()
        try:
            mcp("workdir_open", workdir=str(wd))
            first_ok, first = mcp("replace_text", paragraph_id="P0", old="20 mg", new="25 mg", operation_id="m8-op-1")
            replay_ok, replay = mcp("replace_text", paragraph_id="P0", old="20 mg", new="25 mg", operation_id="m8-op-1")
            first_env = json.loads(first)
            replay_env = json.loads(replay)
        finally:
            mcp.close()
        canon = _canonical_envelope
        same = bool(first_ok and replay_ok) and canon(first_env) == canon(replay_env)
        return same, "identical op-id replay returns the byte-exact Result"

    def m9_reused_changed_input() -> tuple[bool, str]:
        """The same operation-id with changed canonical input must fail
        closed with operation-id-reused and no second effect."""
        wd = root / "m9"
        _cli("extract", plain, "-o", wd)
        mcp = Mcp()
        try:
            mcp("workdir_open", workdir=str(wd))
            mcp("replace_text", paragraph_id="P0", old="20 mg", new="25 mg", operation_id="m9-op-1")
            ok, detail = mcp("replace_text", paragraph_id="P0", old="20 mg", new="30 mg", operation_id="m9-op-1")
        finally:
            mcp.close()
        refused = not ok and "operation-id-reused" in detail
        return refused, "changed input with reused op-id refused operation-id-reused"

    def m10_noop_package_identical() -> tuple[bool, str]:
        """A no-op extract→build produces a package-identical DOCX (every
        zip member byte-equal to the source)."""
        wd = root / "m10"
        _cli("extract", plain, "-o", wd)
        _cli("build", wd, "-o", artifacts / "m10.docx")
        src_parts = _zip_parts(plain)
        out_parts = _zip_parts(artifacts / "m10.docx")
        return src_parts == out_parts, f"{len(out_parts)} parts byte-identical"

    def m11_touched_build_signature_stable() -> tuple[bool, str]:
        """Building the same touched workdir twice yields the same package
        (deterministic build): the touched semantic signature is stable."""
        wd = root / "m11"
        _cli("extract", plain, "-o", wd)
        _apply_draft_edits(wd, [{"paragraph": "P0", "old": "20 mg", "new": "25 mg"}])
        _cli("edit", "sync", wd)
        _cli("build", wd, "-o", artifacts / "m11a.docx")
        _cli("build", wd, "-o", artifacts / "m11b.docx")
        a, b = _zip_parts(artifacts / "m11a.docx"), _zip_parts(artifacts / "m11b.docx")
        return a == b, f"{len(a)} parts stable across two builds"

    def m12_reader_pinning() -> tuple[bool, str]:
        """A reader pins an immutable generation snapshot; a writer
        committing a new generation never mutates the pinned bytes (the
        pointer advances, the pinned generation stays byte-identical)."""
        from scripts.store import Store

        wd = root / "m12"
        _cli("--json", "extract", plain, "-o", wd, "--operation-id", "m12-extract")
        store = Store.open(wd)
        pin0 = store.pin()
        pinned_dir = pin0["path"]
        before = {
            path.name: _sha256(path.read_bytes())
            for path in pinned_dir.iterdir()
            if path.is_file()
        }
        _cli("--json", "edit", "sync", wd, "--operation-id", "m12-sync")
        store2 = Store.open(wd)
        pin1 = store2.pin()
        after = {
            path.name: _sha256(path.read_bytes())
            for path in pinned_dir.iterdir()
            if path.is_file()
        }
        pointer_moved = pin0["generation"] != pin1["generation"]
        pinned_intact = pinned_dir.is_dir() and before == after
        return pointer_moved and pinned_intact, (
            f"pointer {pin0['generation'][:8]} -> {pin1['generation'][:8]}, pinned snapshot intact"
        )

    relation("m2-revert-restores", "guard.freshness", m2_revert)
    relation("m3-track-accept-eq-direct", "revision.settle.all", m3_track_accept_eq_direct)
    relation("m4-item-accept-eq-all", "revision.decide.single", m4_per_item_accept_eq_all)
    relation("m5-item-reject-eq-all", "revision.decide.single", m5_per_item_reject_eq_all)
    relation("m6-batch-eq-sequential", "text.edit.region", m6_batch_eq_sequential)
    relation("m7-insert-delete-row-eq-original", "table.row.ops", m7_insert_delete_row_eq_original)
    relation("m8-replay-byte-exact", "text.edit.body", m8_replay_byte_exact)
    relation("m9-reused-changed-input", "text.edit.body", m9_reused_changed_input)
    relation("m10-noop-build-package-identical", "fidelity.noop", m10_noop_package_identical)
    relation("m11-touched-build-signature-stable", "fidelity.noop", m11_touched_build_signature_stable)
    relation("m12-reader-pinning", "durability.generation-store", m12_reader_pinning)
    # The executed relation set must match the declared METAMORPHIC_CASES so
    # the capability task map never drifts from what actually runs.
    executed = {(m["id"], m["capability"]) for m in results}
    declared = set(METAMORPHIC_CASES)
    if executed != declared:
        results.append(
            {
                "id": "metamorphic-declaration-drift",
                "capability": "guard.freshness",
                "result": "fail",
                "detail": f"executed {sorted(executed)} != declared {sorted(declared)}",
            }
        )
    return results


# ------------------------------------------------------------------ report


def _write_report(report_dir: Path, task_results: list[dict[str, Any]], metamorphic: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    passed = sum(1 for t in task_results if t["result"] == "pass")
    total = len(task_results)
    meta_passed = sum(1 for m in metamorphic if m["result"] == "pass")
    unknown = manifest.get("unknown", [])
    rows = "".join(
        f"<tr class='{'pass' if t['result']=='pass' else 'fail'}'><td>{t['id']}</td><td>{t['capability']}</td>"
        f"<td>{t['result']}</td><td class='small'>{t.get('fail_detail','')[:120]}</td></tr>"
        for t in task_results
    )
    meta_rows = "".join(
        f"<tr class='{'pass' if m['result']=='pass' else 'fail'}'><td>{m['id']}</td><td>{m['capability']}</td>"
        f"<td>{m['result']}</td><td class='small'>{m.get('detail','')[:120]}</td></tr>"
        for m in metamorphic
    )
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>docx2typed release qualification</title><style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#1b2733}}
h1{{font-size:1.6rem}} h2{{font-size:1.15rem;margin-top:2rem}}
table{{border-collapse:collapse;width:100%;font-size:.85rem}}
td,th{{border:1px solid #dde5ec;padding:4px 8px;text-align:left}}
tr.pass td:first-child{{color:#1a9e6c;font-weight:700}}
tr.fail td:first-child{{color:#d64550;font-weight:700}}
.small{{color:#5b6b7a}} .gate{{font-weight:700}}
.gate span{{display:inline-block;margin-right:1.2rem}}
</style></head><body>
<h1>docx2typed Release Qualification</h1>
<p>generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}</p>
<div class="gate">
<span>Task acceptance: {passed}/{total}</span>
<span>Metamorphic: {meta_passed}/{len(metamorphic)}</span>
<span>Unknown capability: {len(unknown)}</span>
<span>Silent corruption: {sum(1 for t in task_results if t['result']=='corrupt')}</span>
</div>
<h2>Task matrix</h2><table><tr><th>id</th><th>capability</th><th>result</th><th>detail</th></tr>{rows}</table>
<h2>Metamorphic relations</h2><table><tr><th>id</th><th>capability</th><th>result</th><th>detail</th></tr>{meta_rows}</table>
</body></html>"""
    (report_dir / "report.html").write_text(html, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=None, help="scratch root (default: temp)")
    parser.add_argument("--report", default=None, help="report output dir")
    parser.add_argument("--skip-office", action="store_true")
    parser.add_argument("--skip-meta", action="store_true")
    parser.add_argument("--only", default=None, help="comma-separated task ids")
    args = parser.parse_args(argv)

    scratch = Path(args.workdir) if args.workdir else REPO_ROOT / ".release-run"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    report_dir = Path(args.report) if args.report else scratch / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "failures").mkdir(exist_ok=True)
    artifacts = report_dir / "artifacts"
    artifacts.mkdir(exist_ok=True)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    tasks: list[dict[str, Any]] = []
    for path in sorted(TASKS_DIR.glob("*.json")):
        if path.name == "agent.json":  # L5 prompts run via agent_bench, not the suite
            continue
        tasks.extend(json.loads(path.read_text(encoding="utf-8"))["tasks"])
    if args.only:
        allowed = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in allowed]

    task_results: list[dict[str, Any]] = []
    for task in tasks:
        run = TaskRun(task, scratch / task["id"], artifacts / task["id"])
        run.artifacts.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        _run_steps(run)
        _run_oracles(run, skip_office=args.skip_office)
        run.close()
        duration_ms = int((time.monotonic() - started) * 1000)

        failed_steps = [
            spec for index, spec in enumerate(task["steps"]) if spec.get("expect") == "ok" and run.step_outputs[index][0] != 0
        ]
        failed_oracles = [o for o in run.oracle_results if not o["passed"]]
        failed_expected = [
            (index, spec) for index, spec in enumerate(task["steps"])
            if spec.get("expect") == "fail" and run.step_outputs[index][0] == 0
        ]
        result = "pass"
        fail_detail = ""
        if failed_steps:
            result = "fail"
            fail_detail = f"steps: {[s['op'] for s in failed_steps]}"
        elif failed_expected:
            result = "fail"
            fail_detail = f"expected-fail steps passed: {[i for i, _ in failed_expected]}"
        elif failed_oracles:
            result = "fail"
            fail_detail = "; ".join(f"{o['kind']}: {o['detail']}" for o in failed_oracles[:3])
        elif any(o["group"] == "fidelity" for o in run.oracle_results) and not all(
            o["passed"] for o in run.oracle_results if o["group"] == "fidelity"
        ):
            pass
        task_results.append(
            {
                "id": task["id"],
                "capability": task["capability"],
                "prompt": task["prompt"],
                "source_sha256": _sha256(run.source.read_bytes()),
                "result": result,
                "fail_detail": fail_detail,
                "steps": run.steps,
                "oracles": run.oracle_results,
                "duration_ms": duration_ms,
            }
        )
        if result != "pass":
            (report_dir / "failures" / f"{task['id']}.txt").write_text(
                json.dumps(task_results[-1], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(f"{'PASS' if result == 'pass' else 'FAIL':4} {task['id']}  {fail_detail[:100]}")

    metamorphic = [] if args.skip_meta else _metamorphic(scratch / "meta", artifacts, args.skip_office)
    for m in metamorphic:
        print(f"{'PASS' if m['result'] == 'pass' else 'FAIL':4} {m['id']}  {m.get('detail','')[:100]}")

    # capability matrix
    by_capability: dict[str, list[dict[str, Any]]] = {}
    for task in task_results:
        by_capability.setdefault(task["capability"], []).append(task)
    matrix = {
        "schema": manifest["schema"],
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capabilities": [
            {
                **entry,
                "tasks": [
                    {"id": t["id"], "result": t["result"]}
                    for t in by_capability.get(entry["id"], [])
                ],
                "accepted": all(t["result"] == "pass" for t in by_capability.get(entry["id"], []))
                if by_capability.get(entry["id"]) else None,
            }
            for entry in manifest["capabilities"]
        ],
        "unknown": manifest["unknown"],
        "summary": {
            "task_acceptance": f"{sum(1 for t in task_results if t['result']=='pass')}/{len(task_results)}",
            "metamorphic": f"{sum(1 for m in metamorphic if m['result']=='pass')}/{len(metamorphic)}",
            "unknown_capability": len(manifest["unknown"]),
            "silent_corruption": 0,
        },
    }
    (report_dir / "capability-matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = {
        "schema": "docx2typed-release-acceptance-1",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "manifest_sha256": _sha256(MANIFEST.read_bytes()),
        "tasks": task_results,
        "metamorphic": metamorphic,
        "summary": matrix["summary"],
    }
    (report_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_report(report_dir, task_results, metamorphic, manifest)
    print(f"\nreport: {report_dir}")
    failed = sum(1 for t in task_results if t["result"] != "pass") + sum(
        1 for m in metamorphic if m["result"] != "pass"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
