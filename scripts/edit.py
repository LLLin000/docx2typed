"""docx2typed edit — hash-bound clean edit projection and freshness gates.

Slice A of the clean-edit contract (issue #4): the Agent-facing ``edit.md``
projection, the authoritative ``edit.state.json`` sidecar, the four freshness
states (clean / dirty / stale-clean / conflict), safe refresh, clean no-op
sync, and build/validate/verify freshness gates. Dirty prose synchronization
is intentionally not implemented in this slice.

Layout
------
- ``typed.md``                  canonical restricted typed AST serialization
- ``edit.md``                   generated span-free Agent projection (patch input)
- ``edit.state.json``           authoritative freshness binding (CLI-managed)
- ``edit.state.json.run.json``  run evidence for extract/refresh/sync

Authority
---------
The ``edit.md`` header is a visible, human-readable mirror only; freshness is
computed from ``edit.state.json``. Any disagreement between the header and the
sidecar is ``edit-header-tampered``. ``validate``, ``build``, and ``verify``
reject every non-clean state. There is no ``build --ignore-edit`` escape hatch.

Grammar
-------
``edit.md`` is a restricted project grammar, not CommonMark or generic HTML.
Paragraph markers are ``<!--@p id="P3"-->`` with one logical body line;
deletions are ``<!--@delete id="P4"-->``; new paragraphs are
``<!--@new temp="N1" inherit="P3"-->`` (recognized by the parser, not applied
in Slice A). Non-text content is projected as atomic
``\u27e6token kind=... id=...\u27e7`` placeholders or paired
``\u27e6range-start ...\u27e7 ... \u27e6range-end id=...\u27e7`` placeholders.
Literal ``\u27e6``/``\u27e7`` in prose are escaped as ``\\u27E6``/``\\u27E7``
and a literal backslash as ``\\\\``; the parser rejects malformed escapes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from .typed_core import (
        AnchorNode,
        InlineNode,
        Node,
        OpaqueNode,
        RangeNode,
        StyleRegistry,
        TextNode,
        TypedDocument,
        TypedError,
        attr_value,
        merge_adjacent_text,
        parse_attributes,
        parse_typed,
        serialize_typed,
    )
    from .typed_docx import ValidationError, sha256_file, validate_workdir
except ImportError:  # direct script execution has no package context.
    from typed_core import (
        AnchorNode,
        InlineNode,
        Node,
        OpaqueNode,
        RangeNode,
        StyleRegistry,
        TextNode,
        TypedDocument,
        TypedError,
        attr_value,
        merge_adjacent_text,
        parse_attributes,
        parse_typed,
        serialize_typed,
    )
    from typed_docx import ValidationError, sha256_file, validate_workdir

EDIT_SCHEMA_VERSION = 1
SYNC_CONTRACT_VERSION = 1
SEGMENTATION_CONTRACT = "uax29-c1-1/unicode-16.0.0"
EDIT_STATE_SCHEMA = "typed-clean-edit-state-1"
EDIT_EVIDENCE_SCHEMA = "typed-clean-edit-run-evidence-1"

STATE_FILE = "edit.state.json"
PROJECTION_FILE = "edit.md"
EVIDENCE_FILE = "edit.state.json.run.json"

TOKEN_START = "\u27e6"  # left white square bracket
TOKEN_END = "\u27e7"  # right white square bracket
ESC_BACKSLASH = "\\\\"
ESC_LBRACKET = "\\u27E6"
ESC_RBRACKET = "\\u27E7"

_HEADER_RE = re.compile(r"^<!--@edit(.*?)-->$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _stage_text(path: Path, text: str) -> Path:
    """Write ``text`` to a same-directory temp file; returns the temp path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    temp_path.write_text(text, encoding="utf-8", newline="\n")
    return temp_path


def _replace_staged(temp_path: Path, path: Path) -> None:
    os.replace(temp_path, path)


# --------------------------------------------------------------------------
# Projection rendering
# --------------------------------------------------------------------------

def _escape_text(text: str) -> str:
    out: list[str] = []
    for char in text:
        if char == "\\":
            out.append(ESC_BACKSLASH)
        elif char == TOKEN_START:
            out.append(ESC_LBRACKET)
        elif char == TOKEN_END:
            out.append(ESC_RBRACKET)
        else:
            out.append(char)
    return "".join(out)


def _project_node(node: Node) -> str:
    if isinstance(node, TextNode):
        return _escape_text(node.text)
    attrs = {"id": node.token_id, "kind": node.kind, **node.attrs}
    if isinstance(node, InlineNode) and node.style_id:
        attrs["style"] = node.style_id
    ordered = [(key, attrs[key]) for key in ("id", "kind") if key in attrs]
    ordered += sorted((key, value) for key, value in attrs.items() if key not in ("id", "kind"))
    rendered = " ".join(f"{key}={attr_value(value)}" for key, value in ordered)
    if isinstance(node, RangeNode):
        inner = "".join(_project_node(child) for child in node.children)
        return (
            f"{TOKEN_START}range-start {rendered}{TOKEN_END}"
            f"{inner}"
            f"{TOKEN_START}range-end id={attr_value(node.token_id)}{TOKEN_END}"
        )
    return f"{TOKEN_START}token {rendered}{TOKEN_END}"


def _render_header(base_typed_sha256: str, base_projection_sha256: str) -> str:
    attrs = {
        "schema": str(EDIT_SCHEMA_VERSION),
        "sync-contract": str(SYNC_CONTRACT_VERSION),
        "base-typed-sha256": base_typed_sha256,
        "base-projection-sha256": base_projection_sha256,
        "segmentation": SEGMENTATION_CONTRACT,
    }
    return "<!--@edit " + " ".join(f"{key}={attr_value(value)}" for key, value in attrs.items()) + "-->"


def render_edit_projection(document: TypedDocument, *, base_typed_sha256: str) -> str:
    """Render the full ``edit.md`` text for a typed document.

    The header is a single grammar object on the first line. The body hash
    excludes the header, so a placeholder header yields the same body hash as
    the final one (see :func:`edit_body_sha256`).
    """
    blocks: list[str] = []
    for paragraph in document.paragraphs:
        if paragraph.inherit:
            marker = (
                f'<!--@p id={attr_value(paragraph.paragraph_id)} '
                f'inherit={attr_value(paragraph.inherit)}-->'
            )
        else:
            marker = f'<!--@p id={attr_value(paragraph.paragraph_id)}-->'
        body = "".join(_project_node(node) for node in merge_adjacent_text(paragraph.nodes))
        blocks.append(marker + ("\n" + body if body else ""))
    for paragraph_id in document.deletions:
        blocks.append(f'<!--@delete id={attr_value(paragraph_id)}-->')
    body_part = "\n\n".join(blocks)
    full = _render_header(base_typed_sha256, _sha256(b"pending")) + "\n\n" + body_part + "\n"
    body_hash = edit_body_sha256(full)
    return _render_header(base_typed_sha256, body_hash) + "\n\n" + body_part + "\n"


# --------------------------------------------------------------------------
# Projection parsing
# --------------------------------------------------------------------------

@dataclass
class EditProjection:
    header: dict[str, str]
    paragraphs: list[tuple[str, dict[str, str], str]] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)


def edit_body_sha256(text: str) -> str:
    """SHA-256 of the canonical edit body: header excluded, CRLF normalized.

    ``splitlines()`` performs the declared line-ending normalization; the
    first non-empty line must be the ``@edit`` header and is excluded.
    """
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not lines[0].startswith("<!--@edit"):
        raise ValidationError("edit-header-missing: edit.md must start with an @edit header")
    return _sha256("\n".join(lines[1:]).encode("utf-8"))


def _comment_attrs(line: str, prefix: str) -> dict[str, str] | None:
    stripped = line.strip()
    if not stripped.startswith(prefix) or not stripped.endswith("-->"):
        return None
    raw = stripped[len(prefix):-3].strip()
    try:
        return parse_attributes(raw)
    except TypedError as exc:
        raise ValidationError(f"edit-grammar-invalid: {exc}") from exc


def _collect_body(lines: list[str], index: int) -> tuple[str, int]:
    body_lines: list[str] = []
    while index < len(lines) and lines[index].strip():
        stripped = lines[index].strip()
        if stripped.startswith("<!--@p") or stripped.startswith("<!--@delete") or stripped.startswith("<!--@new"):
            break
        body_lines.append(lines[index])
        index += 1
    if len(body_lines) > 1:
        raise ValidationError("edit-grammar-invalid: paragraph body must be one logical source line")
    return (body_lines[0] if body_lines else ""), index


def parse_edit_projection(text: str) -> EditProjection:
    """Parse the restricted edit grammar; raises on malformed structure."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValidationError("edit-header-missing: edit.md is empty")
    match = _HEADER_RE.match(lines[0])
    if not match:
        raise ValidationError("edit-header-missing: edit.md must start with an @edit header")
    header = parse_attributes(match.group(1).strip())
    required = {
        "schema",
        "sync-contract",
        "base-typed-sha256",
        "base-projection-sha256",
        "segmentation",
    }
    if set(header) != required:
        raise ValidationError(
            "edit-header-missing: @edit header must declare schema, sync-contract, "
            "base-typed-sha256, base-projection-sha256, segmentation"
        )
    if header["schema"] != str(EDIT_SCHEMA_VERSION) or header["sync-contract"] != str(SYNC_CONTRACT_VERSION):
        raise ValidationError("edit-state-incompatible: unsupported edit schema or sync contract")
    if header["segmentation"] != SEGMENTATION_CONTRACT:
        raise ValidationError("edit-state-incompatible: unsupported segmentation contract")
    for key in ("base-typed-sha256", "base-projection-sha256"):
        value = header[key]
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValidationError(f"edit-grammar-invalid: {key} must be a SHA-256 hex digest")
    projection = EditProjection(header)
    paragraph_ids: list[str] = []
    index = 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        attrs = _comment_attrs(line, "<!--@delete")
        if attrs is not None:
            if set(attrs) != {"id"} or not attrs["id"]:
                raise ValidationError("edit-grammar-invalid: delete marker requires one non-empty id")
            projection.deletions.append(attrs["id"])
            index += 1
            continue
        attrs = _comment_attrs(line, "<!--@new")
        if attrs is not None:
            if set(attrs) != {"temp", "inherit"} or not attrs["temp"] or not attrs["inherit"]:
                raise ValidationError("edit-grammar-invalid: new marker requires temp and inherit")
            body, index = _collect_body(lines, index + 1)
            projection.paragraphs.append(("new", attrs, body))
            continue
        attrs = _comment_attrs(line, "<!--@p")
        if attrs is None:
            raise ValidationError(f"edit-grammar-invalid: expected paragraph marker at line {index + 1}")
        if "id" not in attrs or not attrs["id"] or not set(attrs).issubset({"id", "inherit"}):
            raise ValidationError("edit-grammar-invalid: paragraph marker requires a non-empty id")
        paragraph_ids.append(attrs["id"])
        body, index = _collect_body(lines, index + 1)
        projection.paragraphs.append(("p", attrs, body))
    if len(paragraph_ids) != len(set(paragraph_ids)):
        raise ValidationError("edit-grammar-invalid: duplicate paragraph ID")
    if len(set(projection.deletions)) != len(projection.deletions):
        raise ValidationError("edit-grammar-invalid: duplicate deletion marker")
    if set(paragraph_ids).intersection(projection.deletions):
        raise ValidationError("edit-grammar-invalid: paragraph cannot be both live and deleted")
    return projection


def _parse_placeholder(raw: str) -> tuple[str, dict[str, str]]:
    parts = raw.split(None, 1)
    keyword = parts[0]
    attrs = parse_attributes(parts[1]) if len(parts) > 1 else {}
    return keyword, attrs


def _sorted_attrs(attrs: dict[str, str], *, exclude: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, value) for key, value in attrs.items() if key not in exclude))


def _parsed_placeholder_sequence(body: str) -> list[tuple[Any, ...]]:
    seq: list[tuple[Any, ...]] = []
    stack: list[str] = []
    cursor = 0
    while True:
        start = body.find(TOKEN_START, cursor)
        if start < 0:
            _validate_escaped_prose(body[cursor:])
            break
        _validate_escaped_prose(body[cursor:start])
        end = body.find(TOKEN_END, start + 1)
        if end < 0:
            raise ValidationError("edit-grammar-invalid: unclosed placeholder")
        keyword, attrs = _parse_placeholder(body[start + 1:end])
        if keyword == "token":
            if not {"id", "kind"}.issubset(attrs) or not attrs["id"] or not attrs["kind"]:
                raise ValidationError("edit-grammar-invalid: token placeholder requires id and kind")
            seq.append(("token", attrs["kind"], attrs["id"], _sorted_attrs(attrs, exclude=("id", "kind"))))
        elif keyword == "range-start":
            if not {"id", "kind"}.issubset(attrs) or not attrs["id"] or not attrs["kind"]:
                raise ValidationError("edit-grammar-invalid: range-start placeholder requires id and kind")
            seq.append(("range-start", attrs["kind"], attrs["id"], _sorted_attrs(attrs, exclude=("id", "kind"))))
            stack.append(attrs["id"])
        elif keyword == "range-end":
            if set(attrs) != {"id"} or not attrs["id"]:
                raise ValidationError("edit-grammar-invalid: range-end placeholder requires one non-empty id")
            if not stack or stack[-1] != attrs["id"]:
                raise ValidationError("edit-grammar-invalid: mismatched or reversed range placeholder")
            seq.append(("range-end", attrs["id"]))
            stack.pop()
        else:
            raise ValidationError(f"edit-grammar-invalid: unknown placeholder keyword: {keyword}")
        cursor = end + 1
    if stack:
        raise ValidationError("edit-grammar-invalid: unclosed range placeholder")
    return seq


def _validate_escaped_prose(text: str) -> str:
    """Validate and unescape literal prose between placeholders."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            if char in (TOKEN_START, TOKEN_END):
                raise ValidationError(
                    "edit-grammar-invalid: literal \u27e6/\u27e7 must use \\u27E6/\\u27E7 escapes"
                )
            out.append(char)
            index += 1
            continue
        if text[index + 1:index + 2] == "\\":
            out.append("\\")
            index += 2
            continue
        segment = text[index + 1:index + 6].lower()
        if segment == "u27e6":
            out.append(TOKEN_START)
            index += 6
            continue
        if segment == "u27e7":
            out.append(TOKEN_END)
            index += 6
            continue
        raise ValidationError("edit-grammar-invalid: invalid escape sequence")
    return "".join(out)


def _projected_node_attrs(node: Node) -> tuple[tuple[str, str], ...]:
    attrs = dict(node.attrs)
    if isinstance(node, InlineNode) and node.style_id:
        attrs["style"] = node.style_id
    return tuple(sorted(attrs.items()))


def _baseline_placeholder_sequence(nodes: Iterable[Node]) -> list[tuple[Any, ...]]:
    seq: list[tuple[Any, ...]] = []

    def walk(items: Iterable[Node]) -> None:
        for node in items:
            if isinstance(node, TextNode):
                continue
            if isinstance(node, RangeNode):
                seq.append(("range-start", node.kind, node.token_id, _projected_node_attrs(node)))
                walk(node.children)
                seq.append(("range-end", node.token_id))
                continue
            seq.append(("token", node.kind, node.token_id, _projected_node_attrs(node)))

    walk(nodes)
    return seq


def validate_protected_structure(workdir: Path, projection: EditProjection, *, sync_mode: bool = False) -> None:
    """Projection placeholders must match the current typed AST.

    In ``sync_mode`` (applying an edit draft) deletions are deliberate
    tombstones rather than mutations: existing tombstones must be kept, new
    ones must target a live paragraph, and every live paragraph marker must
    exist in typed.md with identical placeholders.
    """
    typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
    typed_by_id = {paragraph.paragraph_id: paragraph for paragraph in typed.paragraphs}
    if not sync_mode:
        typed_ids = [paragraph.paragraph_id for paragraph in typed.paragraphs]
        projected_ids = [attrs["id"] for kind, attrs, _ in projection.paragraphs if kind == "p"]
        if projected_ids != typed_ids:
            raise ValidationError("protected-token-mutated: projection paragraph IDs differ from typed.md")
        if projection.deletions != list(typed.deletions):
            raise ValidationError("protected-token-mutated: projection deletion markers differ from typed.md")
    else:
        if not set(typed.deletions).issubset(set(projection.deletions)):
            raise ValidationError(
                "protected-token-mutated: a deletion tombstone was removed from the projection"
            )
        for paragraph_id in projection.deletions:
            if paragraph_id not in typed_by_id and paragraph_id not in typed.deletions:
                raise ValidationError(f"protected-token-mutated: unknown deletion target: {paragraph_id}")
    for kind, attrs, body in projection.paragraphs:
        if kind != "p":
            continue
        paragraph_id = attrs["id"]
        paragraph = typed_by_id.get(paragraph_id)
        if paragraph is None:
            raise ValidationError(f"protected-token-mutated: unknown paragraph marker: {paragraph_id}")
        if attrs.get("inherit", "") != (paragraph.inherit or ""):
            raise ValidationError(f"protected-token-mutated: paragraph {paragraph_id} inherit marker differs")
        baseline = _baseline_placeholder_sequence(paragraph.nodes)
        parsed = _parsed_placeholder_sequence(body)
        if baseline != parsed:
            raise ValidationError(f"protected-token-mutated: paragraph {paragraph_id} placeholders differ from typed.md")


# --------------------------------------------------------------------------
# Authoritative state
# --------------------------------------------------------------------------

def create_edit_state(base_typed_sha256: str, base_projection_sha256: str) -> dict[str, Any]:
    return {
        "schema": EDIT_STATE_SCHEMA,
        "edit_schema_version": EDIT_SCHEMA_VERSION,
        "sync_contract_version": SYNC_CONTRACT_VERSION,
        "segmentation_contract": SEGMENTATION_CONTRACT,
        "base_typed_sha256": base_typed_sha256,
        "base_projection_sha256": base_projection_sha256,
    }


def classify_edit_state(path: str | Path) -> dict[str, Any]:
    """Compute freshness from the authoritative sidecar, never the header."""
    workdir = Path(path).resolve()
    state_path = workdir / STATE_FILE
    if not state_path.exists():
        raise ValidationError(
            "edit-state-missing: edit.state.json not found; run `docx2typed edit refresh --init` "
            "to create the projection and authoritative state"
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"edit-state-incompatible: cannot read edit.state.json: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema") != EDIT_STATE_SCHEMA:
        raise ValidationError("edit-state-incompatible: unexpected edit.state.json schema")
    for key, expected in {
        "edit_schema_version": EDIT_SCHEMA_VERSION,
        "sync_contract_version": SYNC_CONTRACT_VERSION,
        "segmentation_contract": SEGMENTATION_CONTRACT,
    }.items():
        if state.get(key) != expected:
            raise ValidationError(f"edit-state-incompatible: {key} mismatch in edit.state.json")
    for key in ("base_typed_sha256", "base_projection_sha256"):
        value = state.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValidationError(f"edit-binding-mismatch: invalid {key} in edit.state.json")
    edit_path = workdir / PROJECTION_FILE
    if not edit_path.exists():
        raise ValidationError(
            "edit-state-missing: edit.md not found; run `docx2typed edit refresh --init`"
        )
    text = edit_path.read_text(encoding="utf-8")
    projection = parse_edit_projection(text)
    header = projection.header
    binding = {
        "schema": str(EDIT_SCHEMA_VERSION),
        "sync-contract": str(SYNC_CONTRACT_VERSION),
        "base-typed-sha256": state["base_typed_sha256"],
        "base-projection-sha256": state["base_projection_sha256"],
        "segmentation": SEGMENTATION_CONTRACT,
    }
    for key, value in binding.items():
        if header.get(key) != value:
            raise ValidationError(f"edit-header-tampered: edit.md header {key} does not match edit.state.json")
    typed_hash = sha256_file(workdir / "typed.md")
    body_hash = edit_body_sha256(text)
    base_typed = state["base_typed_sha256"]
    base_body = state["base_projection_sha256"]
    if typed_hash == base_typed and body_hash == base_body:
        freshness = "clean"
    elif typed_hash == base_typed:
        freshness = "dirty"
    elif body_hash == base_body:
        freshness = "stale-clean"
    else:
        freshness = "conflict"
    return {
        "state": freshness,
        "typed_sha256": typed_hash,
        "edit_body_sha256": body_hash,
        "base_typed_sha256": base_typed,
        "base_projection_sha256": base_body,
        "edit_schema_version": state["edit_schema_version"],
        "sync_contract_version": state["sync_contract_version"],
        "segmentation_contract": state["segmentation_contract"],
        "projection": projection,
    }


def edit_status(path: str | Path) -> dict[str, Any]:
    """Read-only freshness inspection (exit 0 for all four states)."""
    result = classify_edit_state(path)
    if result["state"] in ("clean", "dirty"):
        validate_protected_structure(Path(path).resolve(), result["projection"])
        result["protected_structure"] = "ok"
    else:
        result["protected_structure"] = "not-checked"
    return result


def require_clean_edit(path: str | Path) -> None:
    """Build/validate/verify gate: only ``clean`` passes."""
    workdir = Path(path).resolve()
    result = classify_edit_state(workdir)
    state = result["state"]
    if state == "clean":
        return
    if state == "dirty":
        validate_protected_structure(workdir, result["projection"], sync_mode=True)
    if state == "dirty":
        raise ValidationError(
            "edit-dirty: edit.md has unapplied changes; run `docx2typed edit sync` to apply them "
            "or `docx2typed edit refresh --discard` to replace the projection"
        )
    if state == "stale-clean":
        raise ValidationError(
            "edit-stale: typed.md changed after the projection was generated; "
            "run `docx2typed edit refresh` first"
        )
    raise ValidationError(
        "edit-conflict: typed.md and edit.md both changed; resolve explicitly before building"
    )


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

def _evidence_path(workdir: Path) -> Path:
    return workdir / EVIDENCE_FILE


def _write_regions(workdir: Path, document: TypedDocument) -> None:
    """Best-effort refresh of the read-only style-region view (regions.md)."""
    try:
        from .edit_sync import render_regions_md

        styles = StyleRegistry.from_json(
            json.loads((workdir / "styles.json").read_text(encoding="utf-8"))
        )
        (workdir / "regions.md").write_text(
            render_regions_md(document, styles), encoding="utf-8", newline="\n"
        )
    except (OSError, TypedError, ValidationError):
        pass  # derived view; the canonical artifacts are the success condition


def _build_evidence(
    *,
    command: str,
    status: str,
    started_at: str,
    state_before: str | None,
    typed_before: str,
    typed_after: str,
    base_projection: str,
    projection_before: str | None,
    projection_after: str,
    discarded: str | None,
    diagnostics: str | list[str] | None,
    changed_ids: list[str] | None = None,
    hunks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if isinstance(diagnostics, str):
        diagnostics_list = [diagnostics]
    elif diagnostics is None:
        diagnostics_list = []
    else:
        diagnostics_list = list(diagnostics)
    hunk_list = list(hunks) if hunks else []
    hunk_report = json.dumps(hunk_list, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema": EDIT_EVIDENCE_SCHEMA,
        "command": command,
        "status": status,
        "started_at": started_at,
        "finished_at": _now(),
        "actor": os.environ.get("DOCX2TYPED_ACTOR", "cli"),
        "edit_schema_version": EDIT_SCHEMA_VERSION,
        "sync_contract_version": SYNC_CONTRACT_VERSION,
        "segmentation_contract": SEGMENTATION_CONTRACT,
        "state_before": state_before,
        "typed_before_sha256": typed_before,
        "typed_after_sha256": typed_after,
        "edit_base_projection_sha256": base_projection,
        "edited_edit_sha256": projection_before,
        "projection_after_sha256": projection_after,
        "discarded_edit_sha256": discarded,
        "changed_paragraph_ids": list(changed_ids or []),
        "hunk_report": hunk_list,
        "hunk_report_sha256": _sha256(hunk_report),
        "diagnostics": diagnostics_list,
    }


def _publish(
    workdir: Path,
    projection_text: str,
    state: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    """Stage evidence first, then publish projection and state, then evidence.

    A failure before the first replacement leaves every artifact untouched. An
    interruption after a flat-file replacement leaves a detectable stale or
    missing state (never a false ``clean``).
    """
    evidence_path = _evidence_path(workdir)
    staged_evidence = _stage_text(evidence_path, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    try:
        staged_projection = _stage_text(workdir / PROJECTION_FILE, projection_text)
        _replace_staged(staged_projection, workdir / PROJECTION_FILE)
        staged_state = _stage_text(workdir / STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        _replace_staged(staged_state, workdir / STATE_FILE)
        _replace_staged(staged_evidence, evidence_path)
    except BaseException:
        try:
            Path(staged_evidence).unlink(missing_ok=True)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def generate_clean_edit(workdir: Path, document: TypedDocument) -> None:
    """Create the projection, authoritative state, and evidence after extract."""
    typed_hash = sha256_file(workdir / "typed.md")
    projection_text = render_edit_projection(document, base_typed_sha256=typed_hash)
    body_hash = edit_body_sha256(projection_text)
    state = create_edit_state(typed_hash, body_hash)
    evidence = _build_evidence(
        command="docx2typed extract",
        status="ok",
        started_at=_now(),
        state_before=None,
        typed_before=typed_hash,
        typed_after=typed_hash,
        base_projection=body_hash,
        projection_before=body_hash,
        projection_after=body_hash,
        discarded=None,
        diagnostics=None,
    )
    _publish(workdir, projection_text, state, evidence)
    _write_regions(workdir, document)


def refresh_edit_projection(
    path: str | Path,
    *,
    init: bool = False,
    discard: bool = False,
) -> Path:
    """Regenerate the projection from the current canonical typed.md.

    ``--init`` creates the projection for a legacy workdir that passes the
    existing validator. A dirty or conflicting projection requires
    ``--discard``, which records the discarded draft hash in evidence.
    """
    workdir = Path(path).resolve()
    started_at = _now()
    command = "docx2typed edit refresh" + (" --init" if init else "") + (" --discard" if discard else "")
    validated = validate_workdir(workdir)
    typed_hash = sha256_file(workdir / "typed.md")
    state_path = workdir / STATE_FILE
    state_before: str | None = None
    projection_before: str | None = None
    base_projection: str | None = None
    discarded_hash: str | None = None
    if state_path.exists():
        if init:
            raise ValidationError("edit-init: edit state already exists; drop --init")
        result = classify_edit_state(workdir)
        state_before = result["state"]
        projection_before = result["edit_body_sha256"]
        base_projection = result["base_projection_sha256"]
        if result["state"] in ("dirty", "conflict") and not discard:
            raise ValidationError(
                f"edit-refresh-requires-discard: projection is {result['state']}; "
                "pass --discard to replace it (the discarded hash is recorded in evidence)"
            )
        if discard and result["state"] in ("dirty", "conflict"):
            discarded_hash = result["edit_body_sha256"]
    projection_text = render_edit_projection(validated.typed, base_typed_sha256=typed_hash)
    body_hash = edit_body_sha256(projection_text)
    state = create_edit_state(typed_hash, body_hash)
    evidence = _build_evidence(
        command=command,
        status="ok",
        started_at=started_at,
        state_before=state_before,
        typed_before=typed_hash,
        typed_after=typed_hash,
        base_projection=base_projection or body_hash,
        projection_before=projection_before,
        projection_after=body_hash,
        discarded=discarded_hash,
        diagnostics=None,
    )
    _publish(workdir, projection_text, state, evidence)
    _write_regions(workdir, validated.typed)
    return state_path


def sync_edit_projection(path: str | Path) -> tuple[Path, list[str], list[str]]:
    """Apply the edited ``edit.md`` draft to the canonical typed AST.

    A clean draft is a validated no-op. A stale or conflicting draft is
    rejected. A dirty draft is synchronized under the Word-like ownership
    policy: every accepted hunk is recorded, ``typed.md``/``edit.md``/the
    authoritative state are published together, and the new canonical state
    passes the full workdir validator before success. Any policy violation
    fails closed without mutating the workdir.
    """
    workdir = Path(path).resolve()
    started_at = _now()
    result = classify_edit_state(workdir)
    state = result["state"]
    diagnostics: list[str] = []
    if state == "dirty":
        try:
            validate_protected_structure(workdir, result["projection"], sync_mode=True)
            from .edit_sync import plan_sync

            typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
            format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
            plan = plan_sync(typed, result["projection"], format_data)
            typed_text = serialize_typed(plan.document)
            typed_hash = _sha256(typed_text.encode("utf-8"))
            projection_text = render_edit_projection(plan.document, base_typed_sha256=typed_hash)
            body_hash = edit_body_sha256(projection_text)
            new_state = create_edit_state(typed_hash, body_hash)
            format_text = _sync_format_records(workdir, format_data, plan)
            evidence = _build_evidence(
                command="docx2typed edit sync",
                status="ok",
                started_at=started_at,
                state_before="dirty",
                typed_before=result["typed_sha256"],
                typed_after=typed_hash,
                base_projection=result["base_projection_sha256"],
                projection_before=result["edit_body_sha256"],
                projection_after=body_hash,
                discarded=None,
                diagnostics=plan.warnings,
                changed_ids=plan.changed_ids,
                hunks=plan.hunks,
            )
            _publish_sync(workdir, typed_text, projection_text, new_state, format_text, evidence)
            _write_regions(workdir, plan.document)
            return workdir / STATE_FILE, plan.warnings, plan.changed_ids
        except ValidationError as exc:
            diagnostics.append(str(exc))
            _write_failure_evidence(workdir, started_at, result, diagnostics)
            raise
    if state == "stale-clean":
        raise ValidationError("edit-stale: typed.md changed; run `docx2typed edit refresh` first")
    if state == "conflict":
        raise ValidationError("edit-conflict: typed.md and edit.md both changed; resolve explicitly")
    validate_workdir(workdir)
    evidence = _build_evidence(
        command="docx2typed edit sync",
        status="ok",
        started_at=started_at,
        state_before="clean",
        typed_before=result["typed_sha256"],
        typed_after=result["typed_sha256"],
        base_projection=result["base_projection_sha256"],
        projection_before=result["edit_body_sha256"],
        projection_after=result["edit_body_sha256"],
        discarded=None,
        diagnostics=None,
    )
    _write_json(_evidence_path(workdir), evidence)  # success condition; raises on failure
    return workdir / STATE_FILE, [], []


def _sync_format_records(
    workdir: Path,
    format_data: dict[str, Any],
    plan: Any,
) -> str | None:
    """Record the post-sync governed baseline for changed existing paragraphs.

    Returns the new format.json text, or None when no existing paragraph
    content changed.
    """
    changed = set(plan.changed_ids)
    new_paragraphs = {paragraph.paragraph_id: paragraph for paragraph in plan.document.paragraphs}
    touched = [
        record
        for record in format_data.get("paragraphs", [])
        if record["id"] in changed and record["id"] in new_paragraphs
    ]
    if not touched:
        return None
    for record in touched:
        paragraph = new_paragraphs[record["id"]]
        from .edit_sync import sync_segments_from_nodes
        from .typed_core import skeleton

        record["sync_segments"] = sync_segments_from_nodes(paragraph.nodes)
        record["sync_skeleton"] = skeleton(paragraph.nodes)
    return json.dumps(format_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _publish_sync(
    workdir: Path,
    typed_text: str,
    projection_text: str,
    state: dict[str, Any],
    format_text: str | None,
    evidence: dict[str, Any],
) -> None:
    """Publish the synced canonical state, then validate it.

    All four artifacts are staged first; on any failure the previous bytes of
    every replaced file are restored. Only after the full validator passes on
    the new state is the run evidence written (a success condition).
    """
    typed_path = workdir / "typed.md"
    edit_path = workdir / PROJECTION_FILE
    state_path = workdir / STATE_FILE
    format_path = workdir / "format.json"
    targets = [typed_path, edit_path, state_path]
    contents = [typed_text, projection_text, json.dumps(state, ensure_ascii=False, indent=2) + "\n"]
    if format_text is not None:
        targets.append(format_path)
        contents.append(format_text)
    backups = {path: path.read_bytes() for path in targets}
    staged: dict[Path, Path] = {}
    try:
        for path, content in zip(targets, contents):
            staged[path] = _stage_text(path, content)
        for path in targets:
            _replace_staged(staged.pop(path), path)
        validate_workdir(workdir)
        classify_edit_state(workdir)
    except BaseException:
        for path in targets:
            try:
                path.write_bytes(backups[path])
            except OSError:
                pass
        for temp in staged.values():
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    _write_json(_evidence_path(workdir), evidence)  # success condition; raises on failure


def _write_failure_evidence(
    workdir: Path,
    started_at: str,
    result: dict[str, Any],
    diagnostics: list[str],
) -> None:
    """Best-effort failure record; never part of a successful command."""
    try:
        evidence = _build_evidence(
            command="docx2typed edit sync",
            status="error",
            started_at=started_at,
            state_before=result["state"],
            typed_before=result["typed_sha256"],
            typed_after=result["typed_sha256"],
            base_projection=result["base_projection_sha256"],
            projection_before=result["edit_body_sha256"],
            projection_after=result["edit_body_sha256"],
            discarded=None,
            diagnostics=diagnostics,
        )
        _write_json(_evidence_path(workdir), evidence)
    except OSError:
        pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_status(result: dict[str, Any]) -> None:
    print(f"state: {result['state']}")
    print(f"typed.md sha256: {result['typed_sha256']}")
    print(f"edit.md body sha256: {result['edit_body_sha256']}")
    print(f"base typed sha256: {result['base_typed_sha256']}")
    print(f"base projection sha256: {result['base_projection_sha256']}")
    print(f"edit schema: {result['edit_schema_version']} (sync contract {result['sync_contract_version']})")
    print(f"segmentation: {result['segmentation_contract']}")
    print(f"protected structure: {result['protected_structure']}")


def edit(argv: list[str] | None = None) -> int:
    """docx2typed edit — hash-bound clean edit projection and freshness gates."""
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="docx2typed edit",
        description=(
            "Hash-bound clean edit projection and freshness gates. `status` is "
            "read-only. `refresh` regenerates the projection from typed.md "
            "(--init for a legacy workdir, --discard to replace a dirty draft). "
            "`sync` validates a clean draft as a no-op; dirty prose "
            "synchronization is not implemented in this slice."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    status_parser = sub.add_parser("status", help="report the edit freshness state (read-only)")
    status_parser.add_argument("workdir", help="typed workdir")
    refresh_parser = sub.add_parser("refresh", help="regenerate the edit projection from typed.md")
    refresh_parser.add_argument("workdir", help="typed workdir")
    refresh_parser.add_argument(
        "--init",
        action="store_true",
        help="initialize the projection for a legacy workdir that has none",
    )
    refresh_parser.add_argument(
        "--discard",
        action="store_true",
        help="replace a dirty or conflicting projection; the discarded hash is recorded in evidence",
    )
    sync_parser = sub.add_parser("sync", help="validate a clean draft (Slice A no-op)")
    sync_parser.add_argument("workdir", help="typed workdir")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            _print_status(edit_status(args.workdir))
            return 0
        if args.command == "refresh":
            state_path = refresh_edit_projection(args.workdir, init=args.init, discard=args.discard)
            print(f"refreshed: {state_path}")
            return 0
        state_path, warnings, changed_ids = sync_edit_projection(args.workdir)
        print(f"synced: {state_path}")
        if changed_ids:
            print("changed paragraphs: " + ", ".join(changed_ids))
        for warning in warnings:
            print(f"warning: {warning}")
        return 0
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(edit())
