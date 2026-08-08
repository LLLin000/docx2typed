"""Agent black-box benchmark (L5): natural-language tasks through MCP only.

The agent (human or model) receives a source DOCX, the MCP tool list, and
one natural-language prompt. It may call the MCP tools via the persistent
serve loop below (``--serve``) and the CLI ``extract``/``verify`` commands;
it must NOT touch typed.md, edit.md, XML, or private functions. The grader
(``--grade``) checks the produced DOCX with the same oracle vocabulary as
the release task suite.

Tasks: capabilities/tasks/agent.json. Run:

    python -m scripts.agent_bench --list
    python -m scripts.agent_bench --serve          # persistent MCP driver
    python -m scripts.agent_bench --grade <task-id> <output.docx> <workdir>
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS = REPO_ROOT / "capabilities" / "tasks" / "agent.json"

SERVE_BANNER = "agent-bench serve ready"


def _load_tasks() -> list[dict[str, Any]]:
    return json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]


def _word_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if re.match(rb"word/.*\.xml$", name.encode())
        }


def _visible(xml: bytes) -> str:
    return "".join(
        m.group(1).decode("utf-8", errors="replace")
        for m in re.finditer(rb"<w:t[^>]*>(.*?)</w:t>", xml, re.S)
    )


def _text_count(path: Path, text: str) -> int:
    needle = text.encode("utf-8")
    return sum(part.count(needle) for part in _word_parts(path).values())


def _residual(path: Path) -> int:
    pattern = re.compile(rb"<w:(ins|del)[ >]")
    return sum(len(pattern.findall(part)) for part in _word_parts(path).values())


def grade(task_id: str, output: Path, workdir: Path) -> dict[str, Any]:
    task = next(t for t in _load_tasks() if t["id"] == task_id)
    results: list[dict[str, Any]] = []

    def check(kind: str, ok: bool, detail: str = "") -> None:
        results.append({"kind": kind, "passed": ok, "detail": detail})

    verify_rc = subprocess.run(
        ["python", "-m", "scripts", "verify", str(workdir), str(output)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).returncode
    check("verify_pass", verify_rc == 0, f"verify rc={verify_rc}")
    for spec in task.get("oracles", {}).get("positive", []):
        if spec["kind"] == "text_count":
            count = _text_count(output, spec["text"])
            check(f"text_count {spec['text']}", count == spec["count"], f"count={count}")
        elif spec["kind"] == "text_visible_contains":
            visible = "".join(_visible(p) for p in _word_parts(output).values())
            check(f"visible {spec['text']}", spec["text"] in visible, "present")
        elif spec["kind"] == "residual_revisions":
            count = _residual(output)
            check("residual_revisions", count == spec["count"], f"residual={count}")
        elif spec["kind"] == "comment_present":
            has = _text_count(output, f'<w:comment w:id="{spec["id"]}"') + _text_count(output, f'w:id="{spec["id"]}"')
            check(f"comment {spec['id']} present", has > 0, f"traces={has}")
        elif spec["kind"] == "comment_absent":
            has = _text_count(output, f'<w:comment w:id="{spec["id"]}"') + _text_count(output, f'w:id="{spec["id"]}"')
            check(f"comment {spec['id']} absent", has == 0, f"traces={has}")
    for spec in task.get("oracles", {}).get("negative", []):
        if spec["kind"] == "text_count":
            count = _text_count(output, spec["text"])
            check(f"absent {spec['text']}", count == spec["count"], f"count={count}")
    for spec in task.get("oracles", {}).get("fidelity", []):
        if spec["kind"] == "parts_unchanged":
            src = _word_parts(Path(REPO_ROOT / task["source"]))
            out = _word_parts(output)
            excepted = set(spec.get("except", []))
            changed = [n for n in src if n not in excepted and src[n] != out.get(n)]
            check("parts_unchanged", not changed, f"changed: {changed[:4]}")
        elif spec["kind"] == "row_count":
            xml = _word_parts(output)["word/document.xml"]
            count = len(re.findall(rb"<w:tr[ >]", xml))
            check("row_count", count == spec["count"], f"rows={count}")
        elif spec["kind"] == "no_text_duplication":
            xml = _word_parts(output)["word/document.xml"]
            src_xml = _word_parts(Path(REPO_ROOT / task["source"]))["word/document.xml"]
            src_cells = [m.group(0) for m in re.finditer(rb"<w:tc>.*?</w:tc>", src_xml, re.S)]
            dup = [c for c in src_cells if xml.count(c) > src_xml.count(c)]
            check("no_text_duplication", not dup, f"duplicated cells: {len(dup)}")
        elif spec["kind"] == "office_open":
            result = subprocess.run(
                [r"C:/Program Files/LibreOffice/program/soffice.exe", "--headless",
                 "--convert-to", "pdf", "--outdir", str(output.parent / "pdf"), str(output)],
                capture_output=True, text=True, timeout=600,
            )
            check("office_open", result.returncode == 0 and "convert" in (result.stdout or result.stderr), "converted")

    passed = all(r["passed"] for r in results)
    return {"task_id": task_id, "prompt": task["prompt"], "result": "pass" if passed else "fail", "oracles": results}


def serve() -> int:
    """Persistent MCP tool loop: one JSON request per line, one RESULT line
    per request. State (workdir session) persists across calls."""
    import importlib

    server = importlib.import_module("scripts.mcp_server")
    print(SERVE_BANNER, flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            out = getattr(server, request["tool"])(**request.get("args", {}))
            print("RESULT " + json.dumps({"ok": True, "data": out}, ensure_ascii=True)[:2000], flush=True)
        except Exception as exc:  # noqa: BLE001 - structured tool failure
            print("RESULT " + json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=True), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--grade", nargs=3, metavar=("TASK_ID", "OUTPUT", "WORKDIR"))
    args = parser.parse_args(argv)
    if args.list:
        for task in _load_tasks():
            print(f"{task['id']}: {task['prompt']}  [{task['source']}]")
        return 0
    if args.serve:
        return serve()
    if args.grade:
        task_id, output, workdir = args.grade
        report = grade(task_id, Path(output), Path(workdir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["result"] == "pass" else 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
