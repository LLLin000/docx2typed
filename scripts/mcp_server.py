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
    from .review_collab import CollaborationError, document_state, external_write_guard, preflight, publish_current, settle_decisions, settlement_plan
    from .review_queue import acknowledge as acknowledge_review, snapshot as review_snapshot, update_event as update_review_event
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
    from review_collab import CollaborationError, document_state, external_write_guard, preflight, publish_current, settle_decisions, settlement_plan  # type: ignore[no-redef]
    from review_queue import acknowledge as acknowledge_review, snapshot as review_snapshot, update_event as update_review_event
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
        self.lock = threading.RLock()
        self.author: str | None = None
        self.track_override: bool | None = None
        self.mode: str | None = None

    def require(self) -> Path:
        if self.workdir is None:
            raise ToolError("workdir-not-open", "no workdir open; call workdir_open first")
        return self.workdir


def _agent_preflight(workdir: Path) -> dict[str, Any]:
    result = preflight(workdir)
    if not result["ready"]:
        raise ToolError(
            "agent-preflight-required",
            json.dumps({"reasons": result["reasons"], "queued_events": result["queued_events"]}, ensure_ascii=False),
        )
    return result

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


def _replace_in_body(
    body: str,
    old: str,
    new: str,
    paragraph_id: str,
    *,
    start_offset: int | None = None,
) -> str:
    """Replace one visible range, optionally anchored by paragraph offset."""
    if "\u27e6" in old or "\u27e7" in old:
        raise ToolError(
            "text-not-found",
            f"{paragraph_id}: old must be visible text without placeholder markers",
        )
    matches = 0
    cursor = 0
    out: list[str] = []
    for kind, raw in _split_chunks(body):
        if kind == "token":
            out.append(raw)
            continue
        visible = _validate_escaped_prose(raw)
        if start_offset is None:
            if old in visible:
                matches += 1
                out.append(_escape_prose(visible.replace(old, new, 1)))
            else:
                out.append(raw)
        else:
            local_start = start_offset - cursor
            anchored = (
                matches == 0
                and 0 <= local_start <= len(visible)
                and visible[local_start:local_start + len(old)] == old
            )
            if anchored:
                matches = 1
                out.append(_escape_prose(
                    visible[:local_start] + new + visible[local_start + len(old):]
                ))
            else:
                out.append(raw)
        cursor += len(visible)
    if matches == 0:
        raise ToolError("text-not-found", f"{paragraph_id}: text {old!r} not found at the target offset")
    if start_offset is None and matches > 1:
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
def _fnv1a_utf16(text: str) -> str:
    """Match the browser's UTF-16 FNV-1a selection fingerprint."""
    digest = 2166136261
    encoded = text.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        digest ^= encoded[index] | (encoded[index + 1] << 8)
        digest = (digest * 16777619) & 0xFFFFFFFF
    return f"fnv1a-{digest:08x}"


def _validate_collab_patch_target(workdir: Path, event: dict[str, Any]) -> None:
    """Fail closed unless a semantic patch still addresses the exact text."""
    paragraph_id = str(event.get("paragraph_id") or event.get("target", {}).get("paragraph_id") or "")
    target = event.get("target")
    if not paragraph_id or not isinstance(target, dict):
        raise ToolError("patch-target", "semantic patch needs a paragraph and target")
    texts, styles = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
    paragraph_text = "".join(texts)
    start = target.get("start_offset")
    end = target.get("end_offset")
    before = str(event.get("before", ""))
    expected = str(target.get("expected_text", ""))
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        raise ToolError("patch-range", "semantic patch offsets are invalid")
    if before != expected or paragraph_text[start:end] != before:
        raise ToolError("patch-precondition", f"{paragraph_id}: expected text no longer matches the current snapshot")
    if paragraph_text[max(0, start - 100):start] != str(target.get("left_context", "")):
        raise ToolError("patch-context-mismatch", f"{paragraph_id}: left context no longer matches the current snapshot")
    if paragraph_text[end:end + 100] != str(target.get("right_context", "")):
        raise ToolError("patch-context-mismatch", f"{paragraph_id}: right context no longer matches the current snapshot")
    paragraph_fingerprint = str(target.get("paragraph_fingerprint", ""))
    if paragraph_fingerprint and paragraph_fingerprint != _fnv1a_utf16(paragraph_text):
        raise ToolError("patch-fingerprint-mismatch", f"{paragraph_id}: paragraph fingerprint changed")
    region_fingerprint = str(target.get("region_fingerprint", ""))
    if region_fingerprint and region_fingerprint != _fnv1a_utf16(before):
        raise ToolError("patch-fingerprint-mismatch", f"{paragraph_id}: selected region fingerprint changed")
    style_region_ids = [str(item) for item in (target.get("style_region_ids") or [])]
    current_style_ids = list(dict.fromkeys(styles))
    if style_region_ids and style_region_ids != current_style_ids:
        raise ToolError("patch-style-mismatch", f"{paragraph_id}: style regions changed; re-read the paragraph")
    if before:
        offsets: list[int] = []
        cursor = 0
        for unit_text in texts:
            offsets.append(cursor)
            cursor += len(unit_text)
        offsets.append(cursor)
        i1 = max(i for i, offset in enumerate(offsets) if offset <= start)
        i2 = max(i for i, offset in enumerate(offsets) if offset < end)
        covered = set(styles[i1:i2 + 1])
        if len(covered) > 1:
            raise ToolError("cross-region-text", f"{paragraph_id}: patch covers multiple style regions")

def _apply_patch_to_draft(
    workdir: Path,
    event: dict[str, Any],
    *,
    validate: bool = True,
) -> None:
    if validate:
        _validate_collab_patch_target(workdir, event)
    paragraph_id = str(event["paragraph_id"])
    header, blocks = _read_edit(workdir)
    index = _find_block(blocks, "p", paragraph_id)
    marker = blocks[index].splitlines()[0]
    body = _block_body(blocks[index])
    target = event["target"]
    new_body = _replace_in_body(
        body,
        str(event["before"]),
        str(event["after"]),
        paragraph_id,
        start_offset=int(target["start_offset"]),
    )
    blocks[index] = marker + ("\n" + new_body if new_body else "")
    _write_edit(workdir, header, blocks)
    _refresh_regions(workdir)



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
        collaboration = document_state(path)
        return _json(
            {
                "workdir": str(path),
                "state": state["state"],
                "edit_mode": session.mode,
                "author": session.author,
                "paragraphs": len(typed.paragraphs),
                "current_snapshot": collaboration["current_snapshot"],
                "staged_snapshot": collaboration["staged_snapshot"],
                "current_matches_filesystem": collaboration["current_matches_filesystem"],
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
        _agent_preflight(workdir)
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
        parent_snapshot = _agent_preflight(workdir)["current_snapshot"]["id"]
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
        collaboration = document_state(workdir)
        published = None
        if not collaboration["current_matches_filesystem"]:
            published = publish_current(
                workdir,
                expected_parent_snapshot=parent_snapshot,
                origin="agent",
                changed_paragraph_ids=[paragraph_id],
            )
        _refresh_regions(workdir)
        return _json(
            {
                "paragraph_id": paragraph_id,
                "edits_applied": len(resolved),
                "state": "clean",
                "current_snapshot": published["current_snapshot"] if published else collaboration["current_snapshot"],
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
        _agent_preflight(workdir)
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
        _agent_preflight(workdir)
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


def _commit_sync_impl(
    workdir: Path,
    *,
    origin: str,
    batch_id: str | None = None,
) -> dict[str, Any]:
    parent_snapshot = _agent_preflight(workdir)["current_snapshot"]["id"]
    _, warnings, changed = sync_edit_projection(
        workdir, track=session.track_override, author=session.author
    )
    collaboration = document_state(workdir)
    published = None
    if changed and not collaboration["current_matches_filesystem"]:
        try:
            published = publish_current(
                workdir,
                expected_parent_snapshot=parent_snapshot,
                origin=origin,
                changed_paragraph_ids=changed,
                batch_id=batch_id,
            )
        except CollaborationError as exc:
            raise ToolError(exc.code, exc.detail) from exc
    return {
        "changed_paragraph_ids": changed,
        "warnings": warnings,
        "edit_mode": session.mode,
        "state": "clean",
        "current_snapshot": published["current_snapshot"] if published else collaboration["current_snapshot"],
    }


@mcp.tool()
def commit_sync() -> str:
    """Apply the draft to the canonical typed AST and publish one CAS snapshot."""
    with session.lock:
        return _json(_commit_sync_impl(session.require(), origin="agent"))


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


def _comments_listing(workdir: Path) -> list[dict[str, str]]:
    """Comment inventory for one workdir: id, author, date, text, anchors.
    Lock-free; callers hold the session lock."""
    import re as _re
    import zipfile

    fmt = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
    comments: list[dict[str, str]] = []
    for record in fmt.get("paragraphs", []):
        if record.get("part_key") == "comments" and not record.get("deleted"):
            entry_id = record.get("part_entry_id")
            if entry_id is not None:
                comments.append({
                    "id": str(entry_id),
                    "paragraph_id": record["id"],
                })
    # anchor mapping: body paragraph records carry token ids; the token
    # table records comment-start anchors with their w:id
    anchors: dict[str, list[str]] = {}
    tokens = fmt.get("tokens", {})
    for record in fmt.get("paragraphs", []):
        if record.get("part_key"):
            continue
        for token_id, _kind in record.get("token_ids", []) or []:
            token = tokens.get(token_id) or {}
            if token.get("kind") == "comment-start":
                attrs = token.get("attrs", {}) or {}
                anchors.setdefault(str(attrs.get("w:id")), []).append(record["id"])
    # author/date/text from the template's comments.xml (read-only)
    template = workdir / fmt.get("template", "_template.docx")
    meta: dict[str, dict[str, str]] = {}
    try:
        with zipfile.ZipFile(template) as archive:
            comments_xml = archive.read("word/comments.xml").decode("utf-8")
        for match in _re.finditer(
            r'<w:comment\s+[^>]*?w:id="(\d+)"[^>]*>.*?</w:comment>',
            comments_xml, _re.S,
        ):
            tag = match.group(0)
            author = _re.search(r'w:author="([^"]*)"', tag)
            date = _re.search(r'w:date="([^"]*)"', tag)
            text = "".join(_re.findall(r"<w:t[^>]*>([^<]*)</w:t>", tag))
            meta[match.group(1)] = {
                "author": author.group(1) if author else "",
                "date": date.group(1) if date else "",
                "text": text,
            }
    except Exception:  # noqa: BLE001 - metadata is best-effort
        pass
    result = []
    for comment in comments:
        comment.update(meta.get(comment["id"], {"author": "", "date": "", "text": ""}))
        comment["anchor_paragraphs"] = anchors.get(comment["id"], [])
        result.append(comment)
    return result


@mcp.tool()
def list_comments() -> str:
    """List every comment in the opened workdir: id, author, date, text,
    and the body paragraphs carrying its anchors. The comment workflow
    (delete_comment, decide_all) addresses comments by id."""
    with session.lock:
        workdir = session.require()
        return _json({"comments": _comments_listing(workdir)})


@mcp.tool()
def get_comment(comment_id: str) -> str:
    """Read one comment: id, author, date, text, and the body paragraphs
    carrying its anchors."""
    with session.lock:
        workdir = session.require()
        for comment in _comments_listing(workdir):
            if comment["id"] == str(comment_id):
                return _json(comment)
        raise ToolError("comment-not-found", f"comment {comment_id} not in the workdir")

@mcp.tool()
def review_preflight() -> str:
    """Return the agent gate, current snapshot, staged snapshot, and wake queue."""
    with session.lock:
        workdir = session.require()
        return _json(preflight(workdir))


@mcp.tool()
def review_state() -> str:
    """Read the collaboration session state without consuming review events."""
    with session.lock:
        return _json(document_state(session.require()))
@mcp.tool()
def review_external_preflight(expected_parent_snapshot: str, operation: str = "import") -> str:
    """Issue a CAS guard for an external import or rollback writer."""
    with session.lock:
        return _json(
            external_write_guard(
                session.require(),
                expected_parent_snapshot=expected_parent_snapshot,
                operation=operation,
            )
        )
@mcp.tool()
def review_settlement_plan(event_ids: list[str] | None = None) -> str:
    """Return mixed accept/reject/defer decisions, patches, and carry-forward guards."""
    with session.lock:
        return _json(settlement_plan(session.require(), [str(item) for item in event_ids] if event_ids else None))

@mcp.tool()
def review_settle(event_ids: list[str] | None = None) -> str:
    """Atomically settle accept/reject decisions and carry deferred items."""
    with session.lock:
        return _json(settle_decisions(session.require(), [str(item) for item in event_ids] if event_ids else None))


def _review_apply_batch(workdir: Path, batch_id: str, requested_event_id: str | None = None) -> dict[str, Any]:
    events = review_snapshot(workdir)["events"]
    requested = next(
        (item for item in events if str(item.get("event_id")) == str(requested_event_id)),
        None,
    ) if requested_event_id else None
    if requested_event_id and requested is None:
        raise ToolError("review-event-not-found", f"review event {requested_event_id} not found")
    if requested and requested.get("type") != "patch":
        raise ToolError("not-a-patch", f"review event {requested_event_id} is not a semantic patch")
    if requested and requested.get("delivery_state") == "applied":
        return {"event": requested, "state": "already-applied"}
    batch_events = [
        item for item in events
        if item.get("type") == "patch"
        and item.get("status") == "queued"
        and (not batch_id or str(item.get("batch_id")) == batch_id)
    ]
    batch_events.sort(
        key=lambda item: int(str(item.get("staged_snapshot", "H0.0")).rsplit(".", 1)[-1])
    )
    if requested and requested not in batch_events:
        raise ToolError("patch-not-queued", f"review event {requested_event_id} is not queued")
    if not batch_events:
        raise ToolError("patch-batch-empty", f"no queued patches in batch {batch_id or '<none>'}")
    state = document_state(workdir)
    if not state["current_matches_filesystem"]:
        raise ToolError("current-snapshot-drift", "typed.md differs from the canonical snapshot")
    expected_parent = state["current_snapshot"]["id"]
    for patch in batch_events:
        if patch.get("parent_snapshot") != expected_parent:
            raise ToolError(
                "patch-parent-mismatch",
                f"patch parent {patch.get('parent_snapshot')} does not match {expected_parent}",
            )
        expected_parent = str(patch.get("staged_snapshot") or expected_parent)
        _validate_collab_patch_target(workdir, patch)
    ranges: dict[str, list[tuple[int, int]]] = {}
    for patch in batch_events:
        target = patch["target"]
        start, end = int(target["start_offset"]), int(target["end_offset"])
        paragraph_id = str(patch["paragraph_id"])
        ranges.setdefault(paragraph_id, []).append((start, end))
    for paragraph_id, paragraph_ranges in ranges.items():
        previous_end = -1
        previous_start = -1
        for start, end in sorted(paragraph_ranges):
            if start < previous_end or (start == previous_start and start == end):
                raise ToolError("patch-overlap", f"{paragraph_id}: overlapping patches require a new selection")
            previous_start, previous_end = start, end
    claimed: list[dict[str, Any]] = []
    try:
        for patch in batch_events:
            claimed.append(update_review_event(workdir, str(patch["event_id"]), {"delivery_state": "in_progress"}))
        for patch in sorted(
            batch_events,
            key=lambda item: (str(item["paragraph_id"]), -int(item["target"]["start_offset"])),
        ):
            _apply_patch_to_draft(workdir, patch, validate=False)
        committed = _commit_sync_impl(workdir, origin="human_ui", batch_id=batch_id or None)
        if not committed["changed_paragraph_ids"]:
            raise ToolError("patch-noop", "human patch batch produced no canonical change")
        updated = [
            update_review_event(
                workdir,
                str(patch["event_id"]),
                {
                    "delivery_state": "applied",
                    "review_decision": "adjusted",
                    "applied_snapshot": committed["current_snapshot"]["id"],
                },
            )
            for patch in batch_events
        ]
        result = {"events": updated, "commit": committed, "state": "applied"}
        if requested_event_id:
            result["event"] = next(item for item in updated if str(item["event_id"]) == str(requested_event_id))
        return result
    except Exception as exc:  # noqa: BLE001 - restore draft and keep the batch queued
        try:
            refresh_edit_projection(workdir, discard=True)
        finally:
            for patch in claimed:
                update_review_event(
                    workdir,
                    str(patch["event_id"]),
                    {"delivery_state": "queued", "last_error": str(exc)},
                )
        raise ToolError("patch-apply-failed", str(exc)) from exc


@mcp.tool()
def review_apply_patch(event_id: str) -> str:
    """Apply the queued human patch batch containing ``event_id`` atomically."""
    with session.lock:
        workdir = session.require()
        event = next(
            (item for item in review_snapshot(workdir)["events"] if str(item.get("event_id")) == str(event_id)),
            None,
        )
        if event is None:
            raise ToolError("review-event-not-found", f"review event {event_id} not found")
        if event.get("delivery_state") == "applied":
            return _json({"event": event, "state": "already-applied"})
        return _json(_review_apply_batch(workdir, str(event.get("batch_id") or ""), str(event_id)))


@mcp.tool()
def review_apply_batch(batch_id: str) -> str:
    """Apply one queued human patch batch as one canonical transaction."""
    with session.lock:
        return _json(_review_apply_batch(session.require(), str(batch_id)))
@mcp.tool()
def review_inbox(include_acknowledged: bool = False) -> str:
    """Read queued review events together with the mandatory agent preflight."""
    with session.lock:
        workdir = session.require()
        queue = review_snapshot(workdir)
        gate = preflight(workdir)
        allowed = {"queued", "acknowledged"} if include_acknowledged else {"queued"}
        events = [event for event in queue["events"] if event.get("status") in allowed]
        batches = sorted({str(event["batch_id"]) for event in events if event.get("batch_id")})
        return _json(
            {
                "preflight": gate,
                "events": events,
                "counts": queue["counts"],
                "wake": {"required": bool(events), "batch_ids": batches, "event_count": len(events)},
            }
        )


@mcp.tool()
def review_ack(event_ids: list[str]) -> str:
    """Acknowledge review events after the agent has consumed them."""
    with session.lock:
        workdir = session.require()
        if not event_ids:
            raise ToolError("event-ids-required", "provide at least one review event id")
        acknowledged = acknowledge_review(workdir, [str(event_id) for event_id in event_ids])
        return _json({"acknowledged": acknowledged, "counts": review_snapshot(workdir)["counts"]})


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
    """Independently verify a built DOCX against the workdir.

    Returns structured evidence: the verification result plus revision and
    comment summaries read from the output package, so the caller does not
    need to unzip the DOCX to confirm tracked edits or comment state."""
    import re as _re

    with session.lock:
        workdir = session.require()
        verify_workdir(workdir, output)
        evidence: dict[str, object] = {"verified": str(output)}
        try:
            import zipfile

            with zipfile.ZipFile(output) as archive:
                names = {
                    name for name in archive.namelist()
                    if _re.match(rb"word/.*\.xml$", name.encode())
                }
                xml = b"".join(archive.read(name) for name in sorted(names))
                comments_xml = archive.read("word/comments.xml") if "word/comments.xml" in names else b""
            ins = len(_re.findall(rb"<w:ins[ >]", xml))
            dels = len(_re.findall(rb"<w:del[ >]", xml))
            authors = sorted(
                {value.decode("utf-8", errors="replace") for value in _re.findall(rb'w:author="([^"]*)"', xml)}
            )
            comment_ids = [
                value.decode("utf-8", errors="replace")
                for value in _re.findall(rb'<w:comment w:id="(\d+)"', comments_xml)
            ]
            evidence["checks"] = {
                "text": "pass", "styles": "pass", "structure": "pass",
                "package": "pass", "revisions": "pass", "comments": "pass",
            }
            evidence["revisions"] = {
                "insert": ins, "delete": dels, "authors": authors,
            }
            evidence["comments"] = {"ids": comment_ids}
        except Exception as exc:  # noqa: BLE001 - evidence is best-effort
            evidence["evidence_error"] = str(exc)
        return _json(evidence)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
