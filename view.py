"""Typed-mode clean, style, and raw projections."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .typed_core import StyleRegistry, TypedError, parse_typed, project_clean, project_style
except ImportError:
    from typed_core import StyleRegistry, TypedError, parse_typed, project_clean, project_style


def view_workdir(path: str | Path, mode: str = "clean", *, markers: bool = True) -> str:
    input_path = Path(path).resolve()
    workdir = input_path if input_path.is_dir() else input_path.parent
    typed_path = workdir / "typed.md"
    if not typed_path.exists():
        raise TypedError(f"typed.md not found in {workdir}")
    source = typed_path.read_text(encoding="utf-8")
    if mode == "raw":
        return source
    document = parse_typed(source)
    if mode == "clean":
        return project_clean(document, markers=markers)
    if mode == "style":
        styles_path = workdir / "styles.json"
        if not styles_path.exists():
            raise TypedError(f"styles.json not found in {workdir}")
        styles = StyleRegistry.from_json(json.loads(styles_path.read_text(encoding="utf-8")))
        return project_style(document, styles, markers=markers)
    raise TypedError(f"unknown view mode: {mode}")


def view(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docx2typed view")
    parser.add_argument("input", help="typed workdir or typed.md")
    parser.add_argument("--mode", choices=("clean", "style", "raw"), default="clean")
    parser.add_argument("-o", "--output", help="write projection to a file")
    parser.add_argument("--no-paragraph-markers", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = view_workdir(args.input, args.mode, markers=not args.no_paragraph_markers)
        if args.output:
            Path(args.output).write_text(result, encoding="utf-8", newline="\n")
            print(f"view: {args.output}")
        else:
            print(result, end="" if result.endswith("\n") else "\n")
    except (OSError, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0
