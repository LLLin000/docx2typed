"""docx2typed acceptance corpus runner.

Drives the full pipeline over every corpus document:
    extract -> validate -> no-op build -> verify -> tracked edit -> build -> verify

Usage:
    python -m scripts.acceptance_corpus [--corpus corpus/real] [--workdir DIR] [--json]

Exit code 0 only when every document passes every stage. Corpus documents are
copies; originals are never touched.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(workdir: Path, *args: str, timeout: int = 1200) -> tuple[int, str]:
    result = subprocess.run(
        ["python", "-m", "scripts", *map(str, args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return result.returncode, (result.stdout or result.stderr).strip()


def run_document(doc: Path, wd: Path) -> dict[str, object]:
    """Full pipeline for one corpus document. Returns a stage report."""
    stages: list[dict[str, object]] = []

    def stage(name: str, *args: str) -> bool:
        started = time.time()
        try:
            rc, out = _run(wd, *args)
        except subprocess.TimeoutExpired:
            stages.append({"stage": name, "status": "TIMEOUT", "detail": ""})
            return False
        ok = rc == 0
        stages.append(
            {
                "stage": name,
                "status": "PASS" if ok else "FAIL",
                "detail": out[:200] if not ok else "",
                "seconds": round(time.time() - started, 1),
            }
        )
        return ok

    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    ok = stage("extract", "extract", str(doc), "-o", str(wd))
    if ok:
        ok = stage("validate", "validate", str(wd))
    noop = wd.parent / f"{doc.stem}-noop.docx"
    if ok:
        ok = stage("build-noop", "build", str(wd), "-o", str(noop))
    if ok:
        ok = stage("verify-noop", "verify", str(wd), str(noop))
    edited = wd.parent / f"{doc.stem}-edited.docx"
    if ok:
        # tracked edit: change the first paragraph's text via edit.md
        edit_path = wd / "edit.md"
        if edit_path.exists():
            text = edit_path.read_text(encoding="utf-8")
            marker = text.find("-->", text.find("<!--@p id="))
            if marker >= 0:
                tail_start = text.find("\n", marker) + 1
                line_end = text.find("\n", tail_start)
                if line_end < 0:
                    line_end = len(text)
                if line_end > tail_start:
                    text = text[:tail_start] + text[tail_start:line_end] + "语" + text[line_end:]
                    edit_path.write_text(text, encoding="utf-8")
        ok = stage("sync-edit", "edit", "sync", str(wd), "--no-track")
    if ok:
        ok = stage("build-edited", "build", str(wd), "-o", str(edited))
    if ok:
        ok = stage("verify-edited", "verify", str(wd), str(edited))
    return {"document": doc.name, "pass": ok, "stages": stages}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="corpus/real", help="corpus directory")
    parser.add_argument("--workdir", default=None, help="scratch workdir (default temp)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args(argv)
    corpus = Path(args.corpus)
    docs = sorted(p for p in corpus.iterdir() if p.suffix.lower() == ".docx")
    if not docs:
        print("corpus empty:", corpus)
        return 1
    scratch = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="docx2typed-corpus-"))
    report: list[dict[str, object]] = []
    failed = 0
    for doc in docs:
        entry = run_document(doc, scratch / doc.stem)
        report.append(entry)
        status = "PASS" if entry["pass"] else "FAIL"
        print(f"{status:4} {doc.name}")
        if not entry["pass"]:
            failed += 1
            for stage in entry["stages"]:  # type: ignore[union-attr]
                if stage["status"] != "PASS":
                    print(f"      {stage['stage']}: {stage['status']} {stage['detail'][:160]}")
    total = len(docs)
    print(f"\n{total - failed}/{total} documents passed")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
