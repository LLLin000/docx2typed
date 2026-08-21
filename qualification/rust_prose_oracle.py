"""Issue #58 differential oracle: drives the Python Reference through a
touched extract -> edit_sync -> build -> verify chain for one fixture, and
computes the semantic signature of any DOCX output (visible text per leaf,
style spans per leaf, opaque count, anchor inventory, per-part hashes).

The gate script (qualification/rust_prose_gate.ps1) runs the Rust chain
(extract -> edit text -> build -> verify) and the Python Reference chain on
the same fixture/edit, then compares:

- semantic signature parity (the `docx2typed-semantic-signature-1` payload,
  computed by this oracle for BOTH outputs with the identical Python model),
- per-part byte identity of untouched parts (each output vs the source),
- S-profile resource budget for the edit-build-verify chain.

Usage:
  python qualification/rust_prose_oracle.py python_touch <fixture> <workdir> <edits.json> [--no-track] [--output <out.docx>]
  python qualification/rust_prose_oracle.py signature <docx>
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import time
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.typed_docx import parse_package_document  # noqa: E402
from scripts.typed_core import visible_text  # noqa: E402


def part_hashes(path: pathlib.Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in sorted(archive.namelist())
        }


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def signature(path: pathlib.Path) -> dict:
    """The semantic signature of one output package, computed with the
    Python Reference model (typed paths, style spans, opaque/anchor
    counts, per-part hashes)."""
    with zipfile.ZipFile(path) as archive:
        parsed = parse_package_document(archive)
    paragraphs = []
    total_opaque = 0
    total_anchor = 0
    for paragraph in parsed.document.paragraphs:
        units: list[list[str]] = []
        opaque = 0
        anchor = 0

        def walk(nodes) -> None:
            nonlocal opaque, anchor
            for node in nodes:
                kind = type(node).__name__
                if kind == "TextNode":
                    units.append([node.style_id, node.text])
                elif kind == "OpaqueNode":
                    opaque += 1
                elif kind == "AnchorNode":
                    anchor += 1
                elif kind in ("RangeNode", "RevisionNode"):
                    walk(node.children)

        walk(paragraph.nodes)
        paragraphs.append(
            {
                "id": paragraph.paragraph_id,
                "visible_text": visible_text(paragraph.nodes),
                "units": units,
                "opaque_count": opaque,
                "anchor_count": anchor,
            }
        )
        total_opaque += opaque
        total_anchor += anchor
    return {
        "schema": "docx2typed-semantic-signature-1",
        "package_sha256": file_sha256(path),
        "paragraphs": paragraphs,
        "opaques": total_opaque,
        "anchors": total_anchor,
        "parts": part_hashes(path),
    }


def _cli(*args: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "scripts", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout + result.stderr)


def _apply_draft_edits(workdir: pathlib.Path, edits: list[dict]) -> None:
    text = (workdir / "edit.md").read_text(encoding="utf-8")
    for edit in edits:
        marker = f'<!--@p id="{edit["paragraph"]}"-->'
        start = text.index(marker)
        end = text.find("\n\n", start)
        if end < 0:
            end = len(text)
        block = text[start:end]
        assert edit["old"] in block, f"{edit['old']!r} not in block {edit['paragraph']}"
        text = text[:start] + block.replace(edit["old"], edit["new"], 1) + text[end:]
    (workdir / "edit.md").write_text(text, encoding="utf-8")


def cmd_python_touch(
    fixture: str,
    workdir: str,
    edits_path: str,
    no_track: bool,
    output: str | None,
    result_path: str | None,
) -> None:
    started = time.monotonic()
    fixture_path = (REPO / fixture).resolve()
    workdir_path = pathlib.Path(workdir).resolve()
    edits = json.loads(pathlib.Path(edits_path).read_text(encoding="utf-8"))
    rc, out = _cli("extract", str(fixture_path), "-o", str(workdir_path))
    if rc != 0:
        print(json.dumps({"ok": False, "stage": "extract", "detail": out[-500:]}))
        return
    _apply_draft_edits(workdir_path, edits)
    sync_args = ["edit", "sync", str(workdir_path)]
    if no_track:
        sync_args.append("--no-track")
    rc, out = _cli(*sync_args)
    if rc != 0:
        print(json.dumps({"ok": False, "stage": "edit_sync", "detail": out[-500:]}))
        return
    output_path = (
        pathlib.Path(output).resolve() if output else workdir_path.parent / "python-out.docx"
    )
    rc, out = _cli("build", str(workdir_path), "-o", str(output_path))
    if rc != 0:
        print(json.dumps({"ok": False, "stage": "build", "detail": out[-500:]}))
        return
    rc, out = _cli("verify", str(workdir_path), str(output_path))
    if rc != 0:
        print(json.dumps({"ok": False, "stage": "verify", "detail": out[-500:]}))
        return
    elapsed = time.monotonic() - started
    result = {
        "ok": True,
        "output": str(output_path),
        "elapsed_s": round(elapsed, 3),
        "signature": signature(output_path),
    }
    payload = json.dumps(result, ensure_ascii=False)
    if result_path:
        pathlib.Path(result_path).write_text(payload, encoding="utf-8")
    print(payload)


def cmd_signature(path: str, file_path: str | None = None) -> None:
    payload = json.dumps(signature(pathlib.Path(path).resolve()), ensure_ascii=False)
    if file_path:
        pathlib.Path(file_path).write_text(payload, encoding="utf-8")
    print(payload)


def main() -> None:
    command = sys.argv[1]
    if command == "python_touch":
        fixture = sys.argv[2]
        workdir = sys.argv[3]
        edits_path = sys.argv[4]
        no_track = "--no-track" in sys.argv
        output = None
        if "--output" in sys.argv:
            output = sys.argv[sys.argv.index("--output") + 1]
        result_path = None
        if "--result-file" in sys.argv:
            result_path = sys.argv[sys.argv.index("--result-file") + 1]
        cmd_python_touch(fixture, workdir, edits_path, no_track, output, result_path)
    elif command == "signature":
        file_path = None
        if "--file" in sys.argv:
            file_path = sys.argv[sys.argv.index("--file") + 1]
        cmd_signature(sys.argv[2], file_path)
    else:
        sys.exit(f"unknown oracle command: {command}")


if __name__ == "__main__":
    main()
