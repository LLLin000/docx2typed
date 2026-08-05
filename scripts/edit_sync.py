"""docx2typed edit sync — governed clean-text synchronization (Slices B and C).

Applies an edited ``edit.md`` draft back to the canonical typed AST. Each
paragraph is diffed against its canonical visible text over protected text
units (Unicode extended grapheme clusters under the pinned UAX29-C1-1
contract plus atomic structural tokens). Equal units keep their baseline
style; inserted and replaced text is assigned a style under the explicit
Word-like policy:

- pure insertion at paragraph offset 0 uses the first visible unit to the
  right;
- any other insertion uses the nearest visible unit to the left (including a
  formatted space), falling back to the right only when no left context
  exists;
- insertion into an empty paragraph uses the recorded ``insertion_style``;
- a replacement wholly inside one effective style keeps that style;
- a local mixed-style replacement is accepted only with an unchanged visible
  anchor on at least one side, no protected-boundary crossing, and unique
  alignment; it uses the selection-start style and records a warning;
- an unanchored mixed full-paragraph rewrite is rejected as
  ``unanchored-mixed-rewrite``;
- genuinely different ownership choices from repeated text fail as
  ``ambiguous-alignment``.

Deletions preserve the styles of all surviving units. ``@new`` and ``@delete``
markers apply paragraph insertion (inheriting ``insertion_style``) and
paragraph deletion. The style registry is never modified.

This module imports ``scripts.edit`` for the projection grammar helpers;
``scripts.edit`` imports this module lazily inside ``sync_edit_projection``.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

try:
    from .edit import (
        TOKEN_END,
        TOKEN_START,
        _parse_placeholder,
        _validate_escaped_prose,
    )
    from .typed_core import (
        InlineNode,
        Node,
        OpaqueNode,
        Paragraph,
        RangeNode,
        TextNode,
        TypedDocument,
        TypedError,
        contains_opaque,
        merge_adjacent_text,
    )
    from .typed_docx import ValidationError
except ImportError:  # direct script execution has no package context.
    from edit import (
        TOKEN_END,
        TOKEN_START,
        _parse_placeholder,
        _validate_escaped_prose,
    )
    from typed_core import (
        InlineNode,
        Node,
        OpaqueNode,
        Paragraph,
        RangeNode,
        TextNode,
        TypedDocument,
        TypedError,
        contains_opaque,
        merge_adjacent_text,
    )
    from typed_docx import ValidationError


# --------------------------------------------------------------------------
# UAX29-C1-1 extended grapheme cluster segmentation (approximation)
# --------------------------------------------------------------------------

def _is_extend(char: str) -> bool:
    return unicodedata.category(char) in ("Mn", "Me")


def _is_spacing_mark(char: str) -> bool:
    return unicodedata.category(char) == "Mc"


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


ZWJ = "\u200d"


def grapheme_clusters(text: str) -> list[str]:
    """Segment text into extended grapheme clusters (UAX29-C1-1 approximation).

    Implements the rules that matter for real prose with
    ``unicodedata`` general categories: CRLF pairs (GB3), Extend and
    SpacingMark continuation (GB9/GB9a), ZWJ-linked emoji sequences (GB11),
    and regional-indicator pairs (GB12/GB13). The segmentation contract is
    recorded in the edit header and sync evidence; it is not a claim of full
    UAX29 conformance for exotic scripts.
    """
    if not text:
        return []
    clusters: list[str] = []
    start = 0
    regional_run = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if index + 1 >= length:
            break
        nxt = text[index + 1]
        if char == "\r" and nxt == "\n":
            index += 1  # GB3: CR x LF stays one cluster
            regional_run = 0
            continue
        if _is_regional_indicator(char):
            regional_run += 1
        else:
            regional_run = 0
        if _is_extend(nxt) or _is_spacing_mark(nxt):
            index += 1  # GB9/GB9a
            continue
        if char == ZWJ or nxt == ZWJ:
            index += 1  # GB11: keep ZWJ-linked sequences together
            continue
        if _is_regional_indicator(char) and _is_regional_indicator(nxt) and regional_run % 2 == 1:
            index += 1  # GB12/GB13: pair regional indicators
            continue
        clusters.append(text[start:index + 1])
        start = index + 1
        index += 1
    clusters.append(text[start:])
    return clusters


# --------------------------------------------------------------------------
# Flat unit model
# --------------------------------------------------------------------------

@dataclass
class Unit:
    """One atomic unit of a paragraph: a text cluster or a structural token."""

    value: tuple[Any, ...]
    style: str | None = None
    token: bool = False
    range_path: tuple[str, ...] = ()
    node: Node | None = None


def _token_value(kind: str, token_id: str, attrs: dict[str, str]) -> tuple[Any, ...]:
    return ("T", kind, token_id, tuple(sorted(attrs.items())))


def _range_start_value(kind: str, token_id: str, attrs: dict[str, str]) -> tuple[Any, ...]:
    return ("RS", kind, token_id, tuple(sorted(attrs.items())))


def _range_end_value(token_id: str) -> tuple[Any, ...]:
    return ("RE", token_id)


def flatten_paragraph(paragraph: Paragraph) -> list[Unit]:
    """Flatten a typed AST paragraph into text/token units with range paths."""
    units: list[Unit] = []

    def walk(items: Iterable[Node], path: tuple[str, ...]) -> None:
        for node in items:
            if isinstance(node, TextNode):
                for cluster in grapheme_clusters(node.text):
                    units.append(Unit(("X", cluster), node.style_id, False, path, node))
            elif isinstance(node, RangeNode):
                attrs = dict(node.attrs)
                units.append(Unit(_range_start_value(node.kind, node.token_id, attrs), None, True, path, node))
                walk(node.children, path + (node.token_id,))
                units.append(Unit(_range_end_value(node.token_id), None, True, path, node))
            else:
                attrs = dict(node.attrs)
                if isinstance(node, InlineNode) and node.style_id:
                    attrs["style"] = node.style_id
                units.append(Unit(_token_value(node.kind, node.token_id, attrs), None, True, path, node))

    walk(paragraph.nodes, ())
    return units


def flatten_edit_body(body: str) -> list[Unit]:
    """Parse an edit.md paragraph body into units (text clusters + tokens).

    Text units carry no style (assigned later); token values must match the
    canonical projection so the diff keeps them aligned.
    """
    units: list[Unit] = []
    stack: list[str] = []
    cursor = 0
    while True:
        start = body.find(TOKEN_START, cursor)
        if start < 0:
            for cluster in grapheme_clusters(_validate_escaped_prose(body[cursor:])):
                units.append(Unit(("X", cluster), None, False, tuple(stack), None))
            break
        for cluster in grapheme_clusters(_validate_escaped_prose(body[cursor:start])):
            units.append(Unit(("X", cluster), None, False, tuple(stack), None))
        end = body.find(TOKEN_END, start + 1)
        if end < 0:
            raise ValidationError("edit-grammar-invalid: unclosed placeholder")
        keyword, attrs = _parse_placeholder(body[start + 1:end])
        if keyword == "token":
            if not {"id", "kind"}.issubset(attrs) or not attrs["id"] or not attrs["kind"]:
                raise ValidationError("edit-grammar-invalid: token placeholder requires id and kind")
            token_id = attrs["id"]
            kind = attrs["kind"]
            rest = {key: value for key, value in attrs.items() if key not in ("id", "kind")}
            units.append(Unit(_token_value(kind, token_id, rest), None, True, tuple(stack), None))
        elif keyword == "range-start":
            if not {"id", "kind"}.issubset(attrs) or not attrs["id"] or not attrs["kind"]:
                raise ValidationError("edit-grammar-invalid: range-start placeholder requires id and kind")
            token_id = attrs["id"]
            kind = attrs["kind"]
            rest = {key: value for key, value in attrs.items() if key not in ("id", "kind")}
            units.append(Unit(_range_start_value(kind, token_id, rest), None, True, tuple(stack), None))
            stack.append(token_id)
        elif keyword == "range-end":
            if set(attrs) != {"id"} or not attrs["id"]:
                raise ValidationError("edit-grammar-invalid: range-end placeholder requires one non-empty id")
            if not stack or stack[-1] != attrs["id"]:
                raise ValidationError("edit-grammar-invalid: mismatched or reversed range placeholder")
            stack.pop()
            units.append(Unit(_range_end_value(attrs["id"]), None, True, tuple(stack), None))
        else:
            raise ValidationError(f"edit-grammar-invalid: unknown placeholder keyword: {keyword}")
        cursor = end + 1
    if stack:
        raise ValidationError("edit-grammar-invalid: unclosed range placeholder")
    return units


# --------------------------------------------------------------------------
# Style ownership policy
# --------------------------------------------------------------------------

def _nearest_text(units: list[Unit], index: int) -> Unit | None:
    for unit in reversed(units[:index]):
        if not unit.token and unit.style:
            return unit
    return None


def _next_text(units: list[Unit], index: int) -> Unit | None:
    for unit in units[index:]:
        if not unit.token and unit.style:
            return unit
    return None


def _char_offsets(units: list[Unit]) -> list[tuple[int, int]]:
    offsets: list[tuple[int, int]] = []
    offset = 0
    for unit in units:
        length = len(unit.value[1]) if not unit.token else 0
        offsets.append((offset, offset + length))
        offset += length
    return offsets


def _hunk_range(offsets: list[tuple[int, int]], start: int, end: int, total: int) -> list[int]:
    """Visible-text char range for a hunk; insertion points clamp to the end."""
    if start >= len(offsets):
        point = total
        return [point, point]
    if end <= start:
        return [offsets[start][0], offsets[start][0]]
    return [offsets[start][0], offsets[end - 1][1]]


def _assign_style(
    base_units: list[Unit],
    i1: int,
    i2: int,
    insertion_style: str,
    paragraph_id: str,
) -> tuple[str, str, str | None]:
    """Assign a style to inserted/replaced text under the caret policy."""
    base = base_units[i1:i2]
    left = _nearest_text(base_units, i1)
    right = _next_text(base_units, i2 if i2 > i1 else i1)
    is_insert = i1 == i2
    if is_insert:
        if i1 == 0:
            style = right.style if right else insertion_style
            reason = "paragraph-start-right-context" if right else "insertion-style-fallback"
            return style, reason, None
        if left is None and right is None:
            raise ValidationError(
                f"protected-context-ambiguous: {paragraph_id}: insertion has no visible text context"
            )
        if left and right and left.value == right.value and left.style != right.style:
            raise ValidationError(
                f"ambiguous-alignment: {paragraph_id}: insertion between equal text with different styles"
            )
        style = left.style if left else right.style
        reason = "left-context" if left else "right-context-fallback"
        return style, reason, None
    styles = {unit.style for unit in base if not unit.token}
    if len(styles) == 1:
        paths = {unit.range_path for unit in base}
        if len(paths) > 1:
            raise ValidationError(
                f"protected-boundary-crossing: {paragraph_id}: replacement spans a protected range boundary"
            )
        return styles.pop(), "single-style-replacement", None
    paths = {unit.range_path for unit in base}
    if len(paths) > 1:
        raise ValidationError(
            f"protected-boundary-crossing: {paragraph_id}: replacement spans a protected range boundary"
        )
    if left is None and right is None:
        if i1 == 0 and i2 == len(base_units):
            raise ValidationError(
                f"unanchored-mixed-rewrite: {paragraph_id}: full rewrite of a mixed-style paragraph"
            )
        raise ValidationError(
            f"mixed-selection-requires-anchor: {paragraph_id}: mixed replacement needs an unchanged anchor"
        )
    if left and right and left.value == right.value and left.style != right.style:
        raise ValidationError(
            f"ambiguous-alignment: {paragraph_id}: replacement between equal text with different styles"
        )
    start = next((unit for unit in base if not unit.token), base[0])
    return start.style, "mixed-selection-start-anchored", (
        f"mixed-style replacement in {paragraph_id} used the selection-start style"
    )


# --------------------------------------------------------------------------
# AST rebuild
# --------------------------------------------------------------------------

def rebuild_paragraph(base_paragraph: Paragraph, units: list[Unit]) -> list[Node]:
    """Rebuild a paragraph node tree from the synced unit sequence."""
    nodes: list[Node] = []
    stack: list[tuple[RangeNode, tuple[str, ...]]] = []
    for unit in units:
        if not unit.token:
            target = stack[-1][0].children if stack else nodes
            target.append(TextNode(unit.style or base_paragraph.base_style, unit.value[1]))
            continue
        if unit.value[0] == "RS":
            node = unit.node
            if not isinstance(node, RangeNode):
                raise ValidationError("internal error: range-start unit lost its node")
            stack.append((node, unit.range_path))
            continue
        if unit.value[0] == "RE":
            if not stack:
                raise ValidationError("internal error: unbalanced range units")
            node, _ = stack.pop()
            parent = stack[-1][0].children if stack else nodes
            parent.append(node)
            continue
        if unit.node is None:
            raise ValidationError("internal error: token unit lost its node")
        target = stack[-1][0].children if stack else nodes
        target.append(unit.node)
    if stack:
        raise ValidationError("internal error: unclosed range units")
    return merge_adjacent_text(nodes)


# --------------------------------------------------------------------------
# Per-paragraph synchronization
# --------------------------------------------------------------------------

def _is_token_mutation(units: Iterable[Unit]) -> bool:
    return any(unit.token for unit in units)


def sync_paragraph(
    paragraph: Paragraph,
    body: str,
    insertion_style: str,
) -> tuple[list[Node], list[dict[str, Any]], list[str]]:
    """Return (new nodes, hunk records, warnings) for one edited paragraph."""
    if contains_opaque(paragraph.nodes):
        baseline_units = flatten_paragraph(paragraph)
        current_units = flatten_edit_body(body)
        if [unit.value for unit in baseline_units] != [unit.value for unit in current_units]:
            raise ValidationError(
                f"opaque-paragraph-mutated: {paragraph.paragraph_id}: visible text cannot change "
                "in a paragraph containing unsupported structure"
            )
        return paragraph.nodes, [], []
    baseline_units = flatten_paragraph(paragraph)
    current_units = flatten_edit_body(body)
    baseline_values = [unit.value for unit in baseline_units]
    current_values = [unit.value for unit in current_units]
    if baseline_values == current_values:
        return paragraph.nodes, [], []
    matcher = SequenceMatcher(None, baseline_values, current_values, autojunk=False)
    opcodes = matcher.get_opcodes()
    baseline_offsets = _char_offsets(baseline_units)
    current_offsets = _char_offsets(current_units)
    output: list[Unit] = []
    hunks: list[dict[str, Any]] = []
    warnings: list[str] = []
    changed = False
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            output.extend(baseline_units[i1:i2])
            continue
        changed = True
        base = baseline_units[i1:i2]
        current = current_units[j1:j2]
        if _is_token_mutation(base) or _is_token_mutation(current):
            raise ValidationError(
                f"protected-token-mutated: {paragraph.paragraph_id}: structural token changed "
                "during text synchronization"
            )
        if not any(not unit.token for unit in current):
            # pure deletion: drop the baseline text, keep everything else.
            hunks.append(
                {
                    "paragraph_id": paragraph.paragraph_id,
                    "operation": "delete",
                    "baseline_range": _hunk_range(baseline_offsets, i1, i2, len("".join(u.value[1] for u in baseline_units if not u.token))),
                    "new_range": _hunk_range(current_offsets, j1, j2, len("".join(u.value[1] for u in current_units if not u.token))),
                    "source_style_set": sorted({unit.style for unit in base if unit.style}),
                    "assigned_style": None,
                    "assignment_reason": "deletion-preserves-survivors",
                    "protected_boundaries": sorted({unit.range_path for unit in base}),
                    "warning": None,
                }
            )
            continue
        style, reason, warning = _assign_style(
            baseline_units, i1, i2, insertion_style, paragraph.paragraph_id
        )
        if warning:
            warnings.append(warning)
        for unit in current:
            output.append(Unit(unit.value, style, False, unit.range_path, None))
        baseline_total = len("".join(u.value[1] for u in baseline_units if not u.token))
        current_total = len("".join(u.value[1] for u in current_units if not u.token))
        hunks.append(
            {
                "paragraph_id": paragraph.paragraph_id,
                "operation": "insert" if i1 == i2 else "replace",
                "baseline_range": _hunk_range(baseline_offsets, i1, i2, baseline_total),
                "new_range": _hunk_range(current_offsets, j1, j2, current_total),
                "source_style_set": sorted({unit.style for unit in base if unit.style}),
                "assigned_style": style,
                "assignment_reason": reason,
                "protected_boundaries": sorted({unit.range_path for unit in base}),
                "warning": warning,
            }
        )
    if not changed:
        return paragraph.nodes, [], []
    return rebuild_paragraph(paragraph, output), hunks, warnings


# --------------------------------------------------------------------------
# Document-level plan
# --------------------------------------------------------------------------

def _next_paragraph_id(used: set[str]) -> str:
    numbers = [int(value[1:]) for value in used if value.startswith("P") and value[1:].isdigit()]
    return f"P{max(numbers, default=-1) + 1}"


def _body_has_tokens(body: str) -> bool:
    return TOKEN_START in body


@dataclass
class SyncPlan:
    document: TypedDocument
    changed_ids: list[str] = field(default_factory=list)
    hunks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    new_ids: list[str] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)


def plan_sync(
    typed: TypedDocument,
    projection: Any,
    format_data: dict[str, Any],
) -> SyncPlan:
    """Build the synced typed document from the edited projection.

    Raises ValidationError with stable diagnostics on any policy violation;
    never mutates files.
    """
    records = {record["id"]: record for record in format_data.get("paragraphs", [])}
    by_id = {paragraph.paragraph_id: paragraph for paragraph in typed.paragraphs}
    used_ids = set(by_id) | set(typed.deletions)
    plan = SyncPlan(TypedDocument(dict(typed.meta)))
    for kind, attrs, body in projection.paragraphs:
        if kind == "new":
            inherit = attrs["inherit"]
            if inherit not in records:
                raise ValidationError(f"unknown inherit paragraph in @new marker: {inherit}")
            if _body_has_tokens(body):
                raise ValidationError(
                    f"new paragraph cannot contain structural tokens: {attrs['temp']}"
                )
            new_id = _next_paragraph_id(used_ids)
            used_ids.add(new_id)
            insertion_style = records[inherit].get("insertion_style") or records[inherit].get("base_style", "")
            text = _validate_escaped_prose(body)
            plan.document.paragraphs.append(
                Paragraph(
                    new_id,
                    records[inherit].get("base_style", ""),
                    [TextNode(insertion_style, text)],
                    inherit=inherit,
                )
            )
            plan.new_ids.append(new_id)
            plan.changed_ids.append(new_id)
            continue
        paragraph_id = attrs["id"]
        paragraph = by_id[paragraph_id]
        record = records.get(paragraph_id)
        insertion_style = (record or {}).get("insertion_style") or paragraph.base_style
        nodes, hunks, warnings = sync_paragraph(paragraph, body, insertion_style)
        new_paragraph = Paragraph(
            paragraph.paragraph_id,
            paragraph.base_style,
            nodes,
            p_open=paragraph.p_open,
            ppr=paragraph.ppr,
            raw_xml=paragraph.raw_xml,
            section_bearing=paragraph.section_bearing,
            editable=paragraph.editable,
            inherit=paragraph.inherit,
            original_index=paragraph.original_index,
        )
        plan.document.paragraphs.append(new_paragraph)
        if hunks:
            plan.hunks.extend(hunks)
            plan.changed_ids.append(paragraph_id)
        plan.warnings.extend(warnings)
    for paragraph_id in projection.deletions:
        if paragraph_id in typed.deletions:
            plan.document.deletions.append(paragraph_id)
            continue
        paragraph = by_id.get(paragraph_id)
        if paragraph is None:
            raise ValidationError(f"unknown paragraph in @delete marker: {paragraph_id}")
        if paragraph.section_bearing or any(
            not isinstance(node, TextNode)
            for node in _iter_nodes(paragraph.nodes)
        ):
            raise ValidationError(
                f"paragraph with protected structure cannot be deleted: {paragraph_id}"
            )
        plan.document.deletions.append(paragraph_id)
        plan.deleted_ids.append(paragraph_id)
        plan.changed_ids.append(paragraph_id)
    return plan


def _iter_nodes(nodes: Iterable[Node]) -> Iterable[Node]:
    for node in nodes:
        yield node
        if isinstance(node, RangeNode):
            yield from _iter_nodes(node.children)


# --------------------------------------------------------------------------
# Sync evidence helpers
# --------------------------------------------------------------------------

def sync_segments_from_nodes(nodes: list[Node]) -> list[list[str]]:
    """[style, text] pairs for a paragraph, stored in format.json as the
    post-sync governed baseline."""
    segments: list[list[str]] = []
    for text, style in _text_segment_pairs(nodes):
        segments.append([style, text])
    return segments


def _text_segment_pairs(nodes: Iterable[Node]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for node in nodes:
        if isinstance(node, TextNode):
            if node.text:
                pairs.append((node.text, node.style_id))
        elif isinstance(node, RangeNode):
            pairs.extend(_text_segment_pairs(node.children))
    return pairs
