"""docx2typed-mcp — MCP server exposing the typed-workdir engine as span-free tools.

The agent-facing surface is visible plain text plus a per-paragraph style
region map. Style ownership is decided by the engine with zero guessing:

- unchanged characters keep their exact style;
- rewritten text inherits the style of the baseline text it replaces — only
  when that range covers a single style region (single-region atomic edit);
- a rewrite covering multiple style regions is rejected with the region
  boundaries and a split-edit suggestion (``mixed-replacement-requires-
  unchanged-text`` / ``unanchored-mixed-rewrite``);
- insertions follow the caret context (left neighbor, paragraph-start right
  neighbor, ``insertion_style`` for empty paragraphs).

Workflow:

    workdir_open -> get_paragraph (style regions are shown by default)
    -> replace_text / insert_paragraph / delete_paragraph (write the draft)
    -> diff_preview (per-hunk style ownership, read-only)
    -> commit_sync (apply the draft, publish canonical state)
    -> build_docx -> verify_output

``revert`` discards the uncommitted draft.

Run as stdio MCP server:

    python -m docx2typed.mcp_server
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

try:
    from .edit import (
        PROJECTION_FILE,
        classify_edit_state,
        refresh_edit_projection,
        sync_edit_projection,
    )
    from .edit_sync import (
        _validate_escaped_prose,
        flatten_paragraph,
        plan_sync,
        render_regions_md,
    )
    from .typed_core import (
        InlineNode,
        OpaqueNode,
        RangeNode,
        StyleRegistry,
        TextNode,
        TypedError,
        parse_typed,
    )
    from .typed_docx import ValidationError, build_workdir, validate_workdir, verify_workdir
except ImportError:  # direct script execution has no package context.
    from edit import (
        PROJECTION_FILE,
        classify_edit_state,
        refresh_edit_projection,
        sync_edit_projection,
    )
    from edit_sync import (
        _validate_escaped_prose,
        flatten_paragraph,
        plan_sync,
        render_regions_md,
    )
    from typed_core import (
        InlineNode,
        OpaqueNode,
        RangeNode,
        StyleRegistry,
        TextNode,
        TypedError,
        parse_typed,
    )
    from typed_docx import ValidationError, build_workdir, validate_workdir, verify_workdir

from mcp.server.fastmcp import FastMCP


class ToolError(TypedError):
    """Structured tool failure: ``code: message``; code is a stable diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

class WorkdirSession:
    def __init__(self) -> None:
        self.workdir: Path | None = None
        self.lock = threading.Lock()
        self.author: str | None = None
        self.track_override: bool | None = None
        self.mode: str | None = None

    def require(self) -> Path:
        if self.workdir is None:
            raise ToolError("workdir-not-open", "no workdir open; call workdir_open first")
        return self.workdir


session = WorkdirSession()
mcp = FastMCP("docx2typed")

_ESC_LBRACKET = "\\u27E6"
_ESC_RBRACKET = "\\u27E7"
_ESC_BACKSLASH = "\\\\"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _escape_prose(text: str) -> str:
    out: list[str] = []
    for char in text:
        if char == "\\":
            out.append(_ESC_BACKSLASH)
        elif char == "\u27e6":
            out.append(_ESC_LBRACKET)
        elif char == "\u27e7":
            out.append(_ESC_RBRACKET)
        else:
            out.append(char)
    return "".join(out)


def _split_chunks(body: str) -> list[tuple[str, str]]:
    """Split a draft body into ("text", raw-prose) and ("token", placeholder)
    chunks. Text chunks keep their escaped form; tokens are atomic."""
    chunks: list[tuple[str, str]] = []
    cursor = 0
    while True:
        start = body.find("\u27e6", cursor)
        if start < 0:
            if body[cursor:]:
                chunks.append(("text", body[cursor:]))
            return chunks
        if start > cursor:
            chunks.append(("text", body[cursor:start]))
        end = body.find("\u27e7", start + 1)
        if end < 0:
            raise ToolError("edit-grammar-invalid", "unclosed placeholder in draft")
        chunks.append(("token", body[start : end + 1]))
        cursor = end + 1


def _token_marker(kind: str) -> str:
    return {"tab": "\u21b9", "br": "\u21b5", "cr": "\u21b5"}.get(kind, f"\u27e6{kind}\u27e7")


def _visible_text(body: str) -> str:
    out: list[str] = []
    for kind, raw in _split_chunks(body):
        if kind == "token":
            match = re.match(r"\u27e6(token|range-start|range-end)(.*)", raw)
            if match:
                keyword, attrs = match.group(1), match.group(2)
                kind_match = re.search(r'kind="([^"]+)"', attrs)
                if keyword == "range-end":
                    out.append("\u27e7")
                elif kind_match:
                    out.append(_token_marker(kind_match.group(1)))
                else:
                    out.append("\u27e6?\u27e7")
            else:
                out.append("\u27e6?\u27e7")
        else:
            out.append(_validate_escaped_prose(raw))
    return "".join(out)


def _replace_in_body(body: str, old: str, new: str, paragraph_id: str) -> str:
    """Replace exactly one occurrence of visible text ``old`` in a draft body.

    Matching operates per unescaped prose chunk (structural tokens are atomic
    separators), so ``old`` cannot span a token. The occurrence must be unique.
    """
    if "\u27e6" in old or "\u27e7" in old:
        raise ToolError(
            "text-not-found",
            f"{paragraph_id}: old must be visible text without placeholder markers",
        )
    matches = 0
    out: list[str] = []
    for kind, raw in _split_chunks(body):
        if kind == "token":
            out.append(raw)
            continue
        visible = _validate_escaped_prose(raw)
        if old in visible:
            matches += 1
            out.append(_escape_prose(visible.replace(old, new, 1)))
        else:
            out.append(raw)
    if matches == 0:
        raise ToolError("text-not-found", f"{paragraph_id}: text {old!r} not found in paragraph")
    if matches > 1:
        raise ToolError(
            "text-ambiguous",
            f"{paragraph_id}: text {old!r} appears {matches} times; provide a longer unique context",
        )
    return "".join(out)


def _paragraph_blocks(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not lines[0].startswith("<!--@edit"):
        raise ToolError("edit-header-missing", "edit.md must start with an @edit header")
    header = lines[0]
    blocks: list[str] = []
    current: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("<!--@p") or stripped.startswith("<!--@new") or stripped.startswith("<!--@delete"):
            if current:
                blocks.append("\n".join(current))
                current = []
            current.append(line)
        elif stripped:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return header, blocks


def _read_edit(workdir: Path) -> tuple[str, list[str]]:
    return _paragraph_blocks((workdir / PROJECTION_FILE).read_text(encoding="utf-8"))


def _write_edit(workdir: Path, header: str, blocks: list[str]) -> None:
    (workdir / PROJECTION_FILE).write_text(
        header + "\n\n" + "\n\n".join(blocks) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _find_block(blocks: list[str], prefix: str, paragraph_id: str) -> int:
    for index, block in enumerate(blocks):
        if block.startswith(f'<!--@{prefix} id="{paragraph_id}"'):
            return index
    raise ToolError("paragraph-not-found", f"paragraph {paragraph_id} not found in the draft")


def _block_body(block: str) -> str:
    return "\n".join(block.splitlines()[1:]) if "\n" in block else ""


def _draft_paragraph_state(workdir: Path, paragraph_id: str, mode: str | None = None) -> tuple[list[str], list[str]]:
    """Current (visible-unit texts, styles) of a draft paragraph.

    Uses the sync engine's dry-run so the regions reflect any uncommitted
    edits, not just the committed typed state. ``mode`` carries the session
    edit mode (track/direct); without it the engine re-infers from the
    source signals, which turns ambiguous for documents with pending
    revisions but trackChanges off and wrongly rejects dirty-draft edits.
    """
    typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
    state = classify_edit_state(workdir)
    if state["state"] == "clean":
        paragraph = next((p for p in typed.paragraphs if p.paragraph_id == paragraph_id), None)
        if paragraph is None:
            raise ToolError("paragraph-not-found", f"paragraph {paragraph_id} not in typed.md")
        units = flatten_paragraph(paragraph)
    else:
        from .edit import parse_edit_projection

        projection = parse_edit_projection((workdir / PROJECTION_FILE).read_text(encoding="utf-8"))
        format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
        try:
            revision_ctx = None
            if mode == "track":
                from .edit import _build_revision_context

                revision_ctx = _build_revision_context(
                    typed, format_data, workdir,
                    mode="track", author=session.author or "Unknown", author_source="session",
                )
            plan = plan_sync(typed, projection, format_data, mode=mode, revision_ctx=revision_ctx)
        except ValidationError as exc:
            raise ToolError("draft-invalid", f"current draft cannot be applied: {exc}") from exc
        paragraph = next((p for p in plan.document.paragraphs if p.paragraph_id == paragraph_id), None)
        if paragraph is None:
            raise ToolError("paragraph-not-found", f"paragraph {paragraph_id} not in the draft")
        units = flatten_paragraph(paragraph)
    texts = [unit.value[1] for unit in units if not unit.token]
    styles = [unit.style for unit in units if not unit.token]
    return texts, styles


def _check_single_region(
    workdir: Path,
    paragraph_id: str,
    old: str,
    texts: list[str],
    styles: list[str],
) -> tuple[int, int]:
    """Locate ``old`` in the paragraph's visible units and require it to cover
    exactly one style region. Returns the unit index range."""
    text = "".join(texts)
    count = text.count(old)
    if count == 0:
        raise ToolError("text-not-found", f"{paragraph_id}: text {old!r} not found in paragraph")
    if count > 1:
        raise ToolError(
            "text-ambiguous",
            f"{paragraph_id}: text {old!r} appears {count} times; provide a longer unique context",
        )
    start = text.index(old)
    end = start + len(old)
    offsets: list[int] = []
    cursor = 0
    for unit_text in texts:
        offsets.append(cursor)
        cursor += len(unit_text)
    offsets.append(cursor)
    i1 = max(i for i, offset in enumerate(offsets) if offset <= start)
    i2 = max(i for i, offset in enumerate(offsets) if offset < end)
    covered = set(styles[i1 : i2 + 1])
    if len(covered) > 1:
        regions = _region_labels(texts, styles)
        raise ToolError(
            "cross-region-text",
            f"{paragraph_id}: {old!r} covers multiple style regions: "
            + " / ".join(regions)
            + ". Edit each region separately (see get_paragraph styles), "
            f"e.g. replace_text({paragraph_id}, '<region-a text>', ...) then "
            f"replace_text({paragraph_id}, '<region-b text>', ...).",
        )
    return i1, i2


def _style_info(workdir: Path, style_id: str) -> dict[str, Any]:
    registry = StyleRegistry.from_json(
        json.loads((workdir / "styles.json").read_text(encoding="utf-8"))
    )
    style = registry.styles.get(style_id)
    return {
        "style_id": style_id,
        "description": style.label if style else style_id,
        "rpr": style.rpr if style else None,
    }


def _merge_regions(texts: list[str], styles: list[str]) -> list[tuple[str, str]]:
    regions: list[tuple[str, str]] = []
    for text, style in zip(texts, styles):
        if regions and regions[-1][1] == style:
            regions[-1] = (regions[-1][0] + text, style)
        else:
            regions.append((text, style))
    return regions


def _resolve_region(edit: dict[str, Any], regions: list[tuple[str, str]], edit_no: int) -> int:
    if "region" in edit:
        index = edit["region"]
        if not isinstance(index, int) or index < 0 or index >= len(regions):
            raise ToolError(
                "region-out-of-range",
                f"edit {edit_no}: region {index} out of range (paragraph has {len(regions)} "
                "regions); re-read regions.md",
            )
        return index
    if "text" in edit:
        anchor = edit["text"]
        style_id = edit.get("style_id")
        matches = [
            index
            for index, region in enumerate(regions)
            if region[0] == anchor and (style_id is None or region[1] == style_id)
        ]
        if not matches:
            raise ToolError(
                "text-not-found",
                f"edit {edit_no}: region text {anchor!r} not found; re-read regions.md",
            )
        if len(matches) > 1:
            raise ToolError(
                "text-ambiguous",
                f"edit {edit_no}: region text {anchor!r} appears {len(matches)} times; "
                "add style_id to disambiguate",
            )
        return matches[0]
    raise ToolError("invalid-edit", f"edit {edit_no}: must specify 'region' index or 'text' anchor")


def _apply_batch_to_body(
    body: str,
    regions: list[tuple[str, str]],
    resolved: list[tuple[int, str | None, str]],
) -> str:
    """Apply one edit per region to a draft body.

    Text chunks and regions must tile the same text; each text chunk is
    rebuilt from the (possibly edited) region texts it covers, in order.
    Tokens keep their positions.
    """
    chunks = _split_chunks(body)
    chunk_texts = [_validate_escaped_prose(raw) for kind, raw in chunks if kind == "text"]
    if "".join(chunk_texts) != "".join(region[0] for region in regions):
        raise ToolError(
            "draft-invalid",
            "draft text structure does not match the region view; re-read regions.md",
        )
    region_bounds: list[tuple[int, int]] = []
    cursor = 0
    for region_text, _ in regions:
        region_bounds.append((cursor, cursor + len(region_text)))
        cursor += len(region_text)
    chunk_bounds: list[tuple[int, int]] = []
    cursor = 0
    for chunk_text in chunk_texts:
        chunk_bounds.append((cursor, cursor + len(chunk_text)))
        cursor += len(chunk_text)

    def chunk_regions(start: int, end: int) -> tuple[int, int]:
        first = next((i for i, (rs, _) in enumerate(region_bounds) if rs < end), 0)
        last = next((i for i in range(len(region_bounds) - 1, -1, -1) if region_bounds[i][1] > start), 0)
        return first, last

    new_region_texts = [region[0] for region in regions]
    for region_index, old, new in resolved:
        if old is None:
            new_region_texts[region_index] = new
            continue
        visible = new_region_texts[region_index]
        count = visible.count(old)
        if count == 0:
            raise ToolError(
                "text-not-found",
                f"edit on region {region_index}: text {old!r} not found in that region",
            )
        if count > 1:
            raise ToolError(
                "text-ambiguous",
                f"edit on region {region_index}: text {old!r} appears {count} times in "
                "the region; provide a longer context",
            )
        new_region_texts[region_index] = visible.replace(old, new, 1)
    out: list[str] = []
    text_index = 0
    for kind, raw in chunks:
        if kind == "token":
            out.append(raw)
            continue
        first, last = chunk_regions(*chunk_bounds[text_index])
        text_index += 1
        out.append(_escape_prose("".join(new_region_texts[first : last + 1])))
    return "".join(out)


def _refresh_regions(workdir: Path) -> None:
    """Best-effort rewrite of regions.md from the current draft (dry-run)."""
    try:
        typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
        state = classify_edit_state(workdir)
        if state["state"] == "clean":
            document = typed
        else:
            from .edit import parse_edit_projection

            projection = parse_edit_projection((workdir / PROJECTION_FILE).read_text(encoding="utf-8"))
            format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
            plan = plan_sync(typed, projection, format_data)
            document = plan.document
        styles = StyleRegistry.from_json(
            json.loads((workdir / "styles.json").read_text(encoding="utf-8"))
        )
        (workdir / "regions.md").write_text(
            render_regions_md(document, styles), encoding="utf-8", newline="\n"
        )
    except Exception:
        pass  # derived view; edit.md remains the source of truth


def _region_labels(texts: list[str], styles: list[str]) -> list[str]:
    regions: list[tuple[str, str]] = []
    for unit_text, style in zip(texts, styles):
        if regions and regions[-1][1] == style:
            regions[-1] = (regions[-1][0] + unit_text, style)
        else:
            regions.append((unit_text, style))
    return [f"{text}[{style[:8]}]" for text, style in regions]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
def workdir_open(workdir: str, author: str | None = None, track: bool | None = None) -> str:
    """Open a typed workdir as the session document; validates it and reports
    its freshness state and effective edit mode. Call once before any other
    tool. ``author`` sets the session revision author (fallback:
    $DOCX2TYPED_AUTHOR, then "Unknown"). ``track`` explicitly selects
    tracked (True) or direct (False) edits; None infers from the three-field
    state (source_track_enabled + pending revisions)."""
    with session.lock:
        path = Path(workdir).resolve()
        if not path.is_dir() or not (path / "typed.md").exists():
            raise ToolError("workdir-not-found", f"not a typed workdir: {path}")
        validate_workdir(path)
        state = classify_edit_state(path)
        session.workdir = path
        session.author = author
        session.track_override = track
        format_data = json.loads((path / "format.json").read_text(encoding="utf-8"))
        typed = parse_typed((path / "typed.md").read_text(encoding="utf-8"))
        from .edit_sync import _document_has_revisions
        from .typed_core import effective_edit_mode

        session.mode = effective_edit_mode(
            source_track_enabled=bool(format_data.get("source_track_enabled")),
            has_pending_revisions=_document_has_revisions(typed),
            explicit=("track" if track else "direct") if track is not None else None,
        )
        return _json(
            {
                "workdir": str(path),
                "state": state["state"],
                "edit_mode": session.mode,
                "author": session.author,
                "paragraphs": len(typed.paragraphs),
            }
        )


@mcp.tool()
def workdir_status() -> str:
    """Freshness state of the opened workdir: clean, dirty, stale-clean, or conflict."""
    with session.lock:
        workdir = session.require()
        state = classify_edit_state(workdir)
        return _json({"state": state["state"], "edit_body_sha256": state["edit_body_sha256"]})


@mcp.tool()
def list_paragraphs() -> str:
    """List draft paragraphs: id, visible-text summary, token count, deletions."""
    with session.lock:
        workdir = session.require()
        header, blocks = _read_edit(workdir)
        paragraphs: list[dict[str, Any]] = []
        for block in blocks:
            marker = block.splitlines()[0].strip()
            match = re.match(r'<!--@(p|new) (?:id|temp)="([^"]+)"', marker)
            if match:
                body = _block_body(block)
                visible = _visible_text(body)
                paragraphs.append(
                    {
                        "id": match.group(2),
                        "kind": match.group(1),
                        "summary": visible[:60],
                        "chars": len(visible),
                        "tokens": body.count("\u27e6"),
                        "deleted": False,
                    }
                )
                continue
            match = re.match(r'<!--@delete id="([^"]+)"', marker)
            if match:
                paragraphs.append(
                    {"id": match.group(1), "kind": "delete", "summary": "[deleted]", "chars": 0, "tokens": 0, "deleted": True}
                )
        return _json({"paragraphs": paragraphs})


@mcp.tool()
def get_paragraph(paragraph_id: str) -> str:
    """Read one paragraph: the draft text and its style regions. Editing is
    region-scoped — replace_text rejects old text spanning regions, and
    batch_edit addresses regions by index — so use the styles array (or
    regions.md) to plan separate edits per region. style_id is authoritative:
    equal style_id = identical formatting; rpr holds the full canonical XML
    (translate it with docs/rpr-reference.md)."""
    with session.lock:
        workdir = session.require()
        header, blocks = _read_edit(workdir)
        index = _find_block(blocks, "p", paragraph_id)
        body = _block_body(blocks[index])
        texts, styles = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
        regions: list[dict[str, Any]] = []
        for unit_text, style in zip(texts, styles):
            if regions and regions[-1]["style_id"] == style:
                regions[-1]["text"] += unit_text
            else:
                regions.append({"text": unit_text, **_style_info(workdir, style)})
        return _json(
            {
                "paragraph_id": paragraph_id,
                "text": body,
                "plain": _visible_text(body),
                "tokens": body.count("\u27e6"),
                "styles": regions,
            }
        )


@mcp.tool()
def replace_text(paragraph_id: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of visible text in a paragraph draft.

    Contract: ``old`` must be unique in the paragraph AND cover a single
    style region (see get_paragraph styles). Text crossing style regions is
    rejected with the region boundaries — edit each region separately, e.g.
    replace_text(P0, '智能响应', '新词') then replace_text(P0, 'ABC', 'XYZ').
    Style ownership is decided by the engine with zero guessing: the region's
    style is preserved, insertions follow the caret context.

    Writes the draft only — run diff_preview then commit_sync."""
    with session.lock:
        workdir = session.require()
        header, blocks = _read_edit(workdir)
        index = _find_block(blocks, "p", paragraph_id)
        marker = blocks[index].splitlines()[0]
        body = _block_body(blocks[index])
        texts, styles = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
        _check_single_region(workdir, paragraph_id, old, texts, styles)
        new_body = _replace_in_body(body, old, new, paragraph_id)
        blocks[index] = marker + ("\n" + new_body if new_body else "")
        _write_edit(workdir, header, blocks)
        _refresh_regions(workdir)
        return _json(
            {
                "paragraph_id": paragraph_id,
                "draft": "dirty",
                "next": "diff_preview to inspect style ownership, then commit_sync",
            }
        )


@mcp.tool()
def batch_edit(paragraph_id: str, edits: list[dict]) -> str:
    """Edit several style regions of one paragraph atomically and immediately.

    Each edit targets exactly one region, addressed either by index
    (recommended, from regions.md / get_paragraph styles) or by text anchor:
      {"region": 1, "new": "..."}                 replace whole region
      {"region": 2, "old": "...", "new": "..."}   replace text inside region
      {"text": "...", "style_id": "...", "new": "..."}   text-anchor addressing
    Edits are applied sequentially, each as a single-region sync (the engine
    needs no style inference because the region is explicit). If any edit
    fails the whole batch is rolled back; on success all edits are committed
    and the workdir is clean. A region may be edited at most once per call."""
    with session.lock:
        workdir = session.require()
        texts, styles = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
        regions = _merge_regions(texts, styles)
        resolved: list[tuple[int, str | None, str]] = []
        seen: set[int] = set()
        for edit_no, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                raise ToolError("invalid-edit", f"edit {edit_no}: must be an object")
            region_index = _resolve_region(edit, regions, edit_no)
            if region_index in seen:
                raise ToolError(
                    "invalid-edit",
                    f"edit {edit_no}: region {region_index} is edited twice; merge the edits",
                )
            seen.add(region_index)
            new = edit.get("new")
            if not isinstance(new, str):
                raise ToolError("invalid-edit", f"edit {edit_no}: missing 'new' text")
            old = edit.get("old")
            if old is not None and not isinstance(old, str):
                raise ToolError("invalid-edit", f"edit {edit_no}: 'old' must be text")
            if old is not None and ("\u27e6" in old or "\u27e7" in old):
                raise ToolError(
                    "text-not-found",
                    f"edit {edit_no}: old must be visible text without placeholder markers",
                )
            resolved.append((region_index, old, new))
        protected = [
            workdir / name
            for name in ("typed.md", PROJECTION_FILE, "edit.state.json", "format.json", "regions.md")
        ]
        backup = {path: path.read_bytes() for path in protected if path.exists()}
        try:
            for region_index, old, new in resolved:
                texts_i, styles_i = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
                regions_i = _merge_regions(texts_i, styles_i)
                if region_index >= len(regions_i):
                    raise ToolError(
                        "region-out-of-range",
                        f"region {region_index} no longer exists in the paragraph; re-read regions.md",
                    )
                header, blocks = _read_edit(workdir)
                index = _find_block(blocks, "p", paragraph_id)
                marker = blocks[index].splitlines()[0]
                body = _block_body(blocks[index])
                new_body = _apply_batch_to_body(body, regions_i, [(region_index, old, new)])
                blocks[index] = marker + ("\n" + new_body if new_body else "")
                _write_edit(workdir, header, blocks)
                sync_edit_projection(workdir)
        except BaseException:
            for path, data in backup.items():
                path.write_bytes(data)
            raise
        _refresh_regions(workdir)
        return _json(
            {
                "paragraph_id": paragraph_id,
                "edits_applied": len(resolved),
                "state": "clean",
                "next": "build_docx to export, or continue editing",
            }
        )


@mcp.tool()
def insert_paragraph(after_id: str, text: str, inherit: str | None = None) -> str:
    """Insert a new paragraph after ``after_id`` in the draft. ``inherit``
    copies the referenced paragraph's insertion style (defaults to
    ``after_id``). Text is visible plain text; structural tokens are not
    allowed in new paragraphs. In track mode the new paragraph carries a
    paragraph-mark insertion revision (R2.5)."""
    with session.lock:
        workdir = session.require()
        if after_id.startswith(("T", "B")) or ("." in after_id) or (inherit or "").startswith(("T", "B")) or "." in (inherit or ""):
            raise ToolError(
                "table-structure-immutable",
                "paragraphs cannot be inserted into tables, text boxes, or "
                "header/footer/note parts; container structure operations are out of scope",
            )
        header, blocks = _read_edit(workdir)
        index = _find_block(blocks, "p", after_id)
        inherit = inherit or after_id
        temps = [
            int(m.group(1))
            for block in blocks
            for m in [re.match(r'<!--@new temp="N(\d+)"', block)]
            if m
        ]
        temp = f"N{max(temps, default=0) + 1}"
        block = f'<!--@new temp="{temp}" inherit="{inherit}"-->\n{_escape_prose(text)}'
        blocks.insert(index + 1, block)
        _write_edit(workdir, header, blocks)
        _refresh_regions(workdir)
        return _json(
            {
                "temp_id": temp,
                "inherit": inherit,
                "draft": "dirty",
                "next": "commit_sync allocates the formal paragraph ID",
            }
        )


@mcp.tool()
def delete_paragraph(paragraph_id: str) -> str:
    """Delete a paragraph from the draft. Paragraphs with protected structure
    (tokens, section boundaries) are rejected by commit_sync. In track mode
    the paragraph stays in the document with a paragraph-mark deletion
    revision (R2.5 merge semantics)."""
    with session.lock:
        workdir = session.require()
        if paragraph_id.startswith(("T", "B")) or ("." in paragraph_id and not paragraph_id.startswith("P")):
            raise ToolError(
                "table-structure-immutable",
                "container and part paragraphs cannot be deleted; container "
                "structure operations are out of scope",
            )
        header, blocks = _read_edit(workdir)
        index = _find_block(blocks, "p", paragraph_id)
        blocks.pop(index)
        blocks.append(f'<!--@delete id="{paragraph_id}"-->')
        _write_edit(workdir, header, blocks)
        _refresh_regions(workdir)
        return _json({"paragraph_id": paragraph_id, "draft": "dirty", "next": "commit_sync"})


@mcp.tool()
def diff_preview() -> str:
    """Dry-run of commit_sync: per changed paragraph, the hunks with style
    ownership (source_style_set -> assigned_styles), warnings, or the
    rejection reason. Read-only; never mutates the workdir. Tracked hunks
    report their generated revisions."""
    with session.lock:
        workdir = session.require()
        state = classify_edit_state(workdir)
        if state["state"] != "dirty":
            return _json({"state": state["state"], "changes": []})
        from .edit import _build_revision_context, parse_edit_projection
        from .edit_sync import _document_has_revisions
        from .typed_core import effective_edit_mode

        text = (workdir / PROJECTION_FILE).read_text(encoding="utf-8")
        projection = parse_edit_projection(text)
        typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
        format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
        try:
            mode = session.mode or effective_edit_mode(
                source_track_enabled=bool(format_data.get("source_track_enabled")),
                has_pending_revisions=_document_has_revisions(typed),
            )
            revision_ctx = (
                _build_revision_context(
                    typed, format_data, workdir, mode=mode,
                    author=session.author or "", author_source="session",
                )
                if mode == "track"
                else None
            )
            plan = plan_sync(
                typed, projection, format_data,
                mode=mode, revision_ctx=revision_ctx,
            )
            return _json(
                {
                    "state": "dirty",
                    "edit_mode": mode,
                    "changed_paragraph_ids": plan.changed_ids,
                    "hunks": plan.hunks,
                    "warnings": plan.warnings,
                }
            )
        except ValidationError as exc:
            return _json({"state": "dirty", "edit_mode": mode, "rejected": str(exc), "changes": []})


@mcp.tool()
def commit_sync() -> str:
    """Apply the draft to the canonical typed AST under the session edit mode
    (set by workdir_open's ``track``/``author`` arguments), re-validate the
    whole workdir, and return changed paragraphs and warnings. The workdir
    returns to clean."""
    with session.lock:
        workdir = session.require()
        _, warnings, changed = sync_edit_projection(
            workdir, track=session.track_override, author=session.author
        )
        return _json(
            {
                "changed_paragraph_ids": changed,
                "warnings": warnings,
                "edit_mode": session.mode,
                "state": "clean",
            }
        )


@mcp.tool()
def accept_revision(revision_key: str, expected_fingerprint: str) -> str:
    """Accept one tracked revision addressed by its revision_key
    (part|kind|w:id|fingerprint, from revisions.json) plus the expected
    fingerprint. Accept insert = unwrap its text; accept delete = remove it.
    Publish transactionally and regenerate all derived views. Requires a
    clean workdir."""
    with session.lock:
        workdir = session.require()
        from .decisions import _decide_single

        decision = _decide_single(
            workdir, revision_key, action="accept", author=session.author,
            expected_fingerprint=expected_fingerprint,
        )
        return _json({"decision": decision, "state": "clean"})


@mcp.tool()
def reject_revision(revision_key: str, expected_fingerprint: str) -> str:
    """Reject one tracked revision addressed by revision_key + fingerprint.
    Reject insert = remove its text; reject delete = restore its text.
    Publish transactionally; requires a clean workdir."""
    with session.lock:
        workdir = session.require()
        from .decisions import _decide_single

        decision = _decide_single(
            workdir, revision_key, action="reject", author=session.author,
            expected_fingerprint=expected_fingerprint,
        )
        return _json({"decision": decision, "state": "clean"})


@mcp.tool()
def reinsert_deleted_text(
    revision_key: str,
    expected_fingerprint: str,
    text: str | None = None,
) -> str:
    """Create a NEW insertion revision after an existing deletion (key +
    fingerprint), without touching the original deletion. ``text`` defaults
    to the deleted text."""
    with session.lock:
        workdir = session.require()
        from .decisions import _decide_single

        decision = _decide_single(
            workdir, revision_key, action="reinsert",
            author=session.author, text=text,
            expected_fingerprint=expected_fingerprint,
        )
        return _json({"decision": decision, "state": "clean"})


@mcp.tool()
def delete_comment(comment_id: str) -> str:
    """Delete one Word comment by its w:id: the comments.xml entry, every
    commentRangeStart/End anchor and commentReference in the document are
    removed. Publishes transactionally; requires a clean workdir."""
    with session.lock:
        workdir = session.require()
        from .decisions import _delete_comment

        decision = _delete_comment(workdir, comment_id)
        return _json({"decision": decision, "state": "clean"})


def _table_op_tool(operation: str, table_ref: str, output: str, workdir_out: str, *numbers: int, discard_content: bool = False) -> str:
    from .decisions import _apply_table_op

    with session.lock:
        workdir = session.require()
        new_workdir = _apply_table_op(
            workdir, table_ref, operation, list(numbers),
            Path(output), Path(workdir_out),
            discard_content=discard_content,
        )
        return _json({"operation": operation, "table": table_ref, "workdir": str(new_workdir)})


@mcp.tool()
def table_insert_row(table_ref: str, after: int, output: str, workdir_out: str) -> str:
    """Insert an empty row after ``after`` (0-based) in ``table_ref`` (T0).
    Produces a new DOCX and clean-baseline workdir; the source is untouched."""
    return _table_op_tool("insert-row", table_ref, output, workdir_out, after)


@mcp.tool()
def table_delete_row(table_ref: str, row: int, output: str, workdir_out: str) -> str:
    """Delete row ``row`` (0-based) from ``table_ref``."""
    return _table_op_tool("delete-row", table_ref, output, workdir_out, row)


@mcp.tool()
def table_insert_col(table_ref: str, after: int, output: str, workdir_out: str) -> str:
    """Insert an empty column after ``after`` (0-based) in every row."""
    return _table_op_tool("insert-col", table_ref, output, workdir_out, after)


@mcp.tool()
def table_delete_col(table_ref: str, col: int, output: str, workdir_out: str) -> str:
    """Delete column ``col`` (0-based) from every row."""
    return _table_op_tool("delete-col", table_ref, output, workdir_out, col)


@mcp.tool()
def table_merge_cells(table_ref: str, row: int, col: int, span: int, output: str, workdir_out: str, discard_content: bool = False) -> str:
    """Merge ``span`` cells horizontally starting at (row, col) via gridSpan.

    Fail-closed: when a spanned cell (beyond the first) carries text, the
    merge is refused with ``merge-would-discard-content`` unless
    ``discard_content=true`` explicitly drops it. The first cell's content
    is always kept."""
    return _table_op_tool("merge-cells", table_ref, output, workdir_out, row, col, span, discard_content=discard_content)


@mcp.tool()
def table_split_cells(table_ref: str, row: int, col: int, span: int, output: str, workdir_out: str) -> str:
    """Split the cell at (row, col) into ``span`` cells."""
    return _table_op_tool("split-cells", table_ref, output, workdir_out, row, col, span)


@mcp.tool()
def decide_all(
    action: str,
    output: str,
    workdir_out: str,
) -> str:
    """Accept or reject every revision and produce a new clean-baseline
    project: build a decided DOCX at ``output`` and re-extract it into a new
    workdir at ``workdir_out`` (normalization governance). The original
    workdir is never mutated. ``action``: accept | reject."""
    with session.lock:
        workdir = session.require()
        from .decisions import _decide_all

        if action not in ("accept", "reject"):
            raise ToolError("invalid-action", "action must be accept or reject")
        new_workdir = _decide_all(workdir, action, Path(output), Path(workdir_out))
        return _json(
            {
                "action": action,
                "output": str(Path(output).resolve()),
                "workdir": str(new_workdir),
                "note": "original workdir untouched; decisions.json in the new workdir",
            }
        )


@mcp.tool()
def revert() -> str:
    """Discard the uncommitted draft and regenerate the projection from the
    canonical typed source (equivalent to edit refresh --discard)."""
    with session.lock:
        workdir = session.require()
        refresh_edit_projection(workdir, discard=True)
        return _json({"state": "clean", "message": "draft discarded"})


@mcp.tool()
def build_docx(output: str | None = None) -> str:
    """Build the DOCX from the committed workdir (requires clean state)."""
    with session.lock:
        workdir = session.require()
        built = build_workdir(workdir, output)
        return _json({"output": str(built)})


@mcp.tool()
def verify_output(output: str) -> str:
    """Independently verify a built DOCX against the workdir."""
    with session.lock:
        workdir = session.require()
        verify_workdir(workdir, output)
        return _json({"verified": str(output)})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
