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
- a local mixed-style replacement is styled by the explicit
  ``proportional-preserve`` policy when it keeps an unchanged visible
  anchor on at least one side with unique alignment: rewritten units are
  mapped to baseline styles by character-position proportion across the
  original region boundaries, and the hunk records the
  ``proportional-preserve`` reason plus a warning - semantic ownership of
  the original styles is NOT preserved, only distributed deterministically;
  protected-boundary crossings still fail as ``protected-boundary-crossing``;
- genuinely different ownership choices from repeated text fail as
  ``ambiguous-alignment``.

Deletions preserve the styles of all surviving units. ``@new`` and ``@delete``
markers apply paragraph insertion (inheriting ``insertion_style``) and
paragraph deletion. The style registry is never modified by an edit; a
``^{…}``/``_{…}`` vertical tag is the one exception — it asks the sync to
synthesize (and register) the missing alignment variant.

This module imports ``scripts.edit`` for the projection grammar helpers;
``scripts.edit`` imports this module lazily inside ``sync_edit_projection``.
"""
from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

try:
    from .edit import (
        TOKEN_END,
        TOKEN_START,
        _VERTICAL_ESCAPE_CHARS,
        _parse_placeholder,
        _validate_escaped_prose,
    )
    from .typed_core import (
        InlineNode,
        Node,
        OpaqueNode,
        Paragraph,
        RevisionNode,
        RangeNode,
        StyleRegistry,
        TextNode,
        TypedDocument,
        TypedError,
        vertical_style_variant,
        contains_opaque,
        merge_adjacent_text,
        visible_text,
        xml_escape,
    )
    from .typed_docx import ValidationError
except ImportError:  # direct script execution has no package context.
    from edit import (
        TOKEN_END,
        TOKEN_START,
        _VERTICAL_ESCAPE_CHARS,
        _parse_placeholder,
        _validate_escaped_prose,
    )
    from typed_core import (
        InlineNode,
        Node,
        OpaqueNode,
        Paragraph,
        RevisionNode,
        RangeNode,
        StyleRegistry,
        TextNode,
        TypedDocument,
        TypedError,
        vertical_style_variant,
        contains_opaque,
        merge_adjacent_text,
        visible_text,
        xml_escape,
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
    vertical: str = ""  # "" | "superscript" | "subscript" (projection tag request)


_VERTICAL_TAGS = {"^": "superscript", "_": "subscript"}
_VERTICAL_ESCAPES = frozenset(_VERTICAL_ESCAPE_CHARS)


def _split_vertical_tags(text: str) -> list[tuple[str, str]]:
    """[(prose, vertical)] segments of one prose chunk.

    ``^{…}`` marks superscript and ``_{…}`` subscript; a backslash escapes
    ``^ _ { }`` and itself. The tag is an editing affordance for the span-free
    projection: the synced state is an ordinary run-properties variant, never
    literal text, and the tag refuses to be ambiguous (empty, unclosed, or
    nested tags fail closed)."""
    segments: list[tuple[str, str]] = []
    buffer: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in _VERTICAL_ESCAPES:
            buffer.append(text[index + 1])
            index += 2
            continue
        direction = _VERTICAL_TAGS.get(char)
        if direction is not None and index + 1 < len(text) and text[index + 1] == "{":
            if buffer:
                segments.append(("".join(buffer), ""))
                buffer = []
            inner, index = _read_vertical_tag(text, index + 2)
            segments.append((inner, direction))
            continue
        buffer.append(char)
        index += 1
    if buffer:
        segments.append(("".join(buffer), ""))
    return segments


def _read_vertical_tag(text: str, start: int) -> tuple[str, int]:
    """One tag's prose (unescaped) and the index just past its closing brace."""
    body: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in _VERTICAL_ESCAPES:
            body.append(text[index + 1])
            index += 2
            continue
        if char == "}":
            if not body:
                raise ValidationError("vertical-tag-empty: a vertical tag carries no text")
            return "".join(body), index + 1
        if _VERTICAL_TAGS.get(char) is not None and index + 1 < len(text) and text[index + 1] == "{":
            raise ValidationError("vertical-tag-nested: a vertical tag cannot contain another tag")
        body.append(char)
        index += 1
    raise ValidationError("vertical-tag-unclosed: a vertical tag is missing its closing brace")


def _append_text_units(units: list[Unit], text: str, path: tuple[str, ...]) -> None:
    """Append the clusters of one prose chunk, remembering tag requests.

    A tagged cluster carries a distinct diff value, so adding a tag to
    unchanged text is still a change the sync can see."""
    for prose, vertical in _split_vertical_tags(text):
        for cluster in grapheme_clusters(prose):
            value = ("V", cluster, vertical) if vertical else ("X", cluster)
            units.append(Unit(value, None, False, path, None, vertical))


def _token_value(kind: str, token_id: str, attrs: dict[str, str]) -> tuple[Any, ...]:
    return ("T", kind, token_id, tuple(sorted(attrs.items())))


def _range_start_value(kind: str, token_id: str, attrs: dict[str, str]) -> tuple[Any, ...]:
    return ("RS", kind, token_id, tuple(sorted(attrs.items())))


def _range_end_value(token_id: str) -> tuple[Any, ...]:
    return ("RE", token_id)


def _gap_value(kind: str, token_id: str) -> tuple[Any, ...]:
    return ("G", kind, token_id)


def _insert_start_value(kind: str, token_id: str) -> tuple[Any, ...]:
    return ("IS", kind, token_id)


def _insert_end_value(token_id: str) -> tuple[Any, ...]:
    return ("IE", token_id)


def flatten_paragraph(paragraph: Paragraph) -> list[Unit]:
    """Flatten a typed AST paragraph into text/token units with range paths.

    Insertion revisions become protected range structures: an open unit, the
    visible text clusters (with the revision id on their path), and a close
    unit — so editing inside an existing insertion stays inside it (R2
    nesting). Deletion revisions become atomic zero-width
    ``("G", kind, token_id)`` units preserving the hidden position.
    """
    units: list[Unit] = []

    def walk(items: Iterable[Node], path: tuple[str, ...]) -> None:
        for node in items:
            if isinstance(node, TextNode):
                for cluster in grapheme_clusters(node.text):
                    value = ("V", cluster, node.vertical) if node.vertical else ("X", cluster)
                    units.append(Unit(value, node.style_id, False, path, node, node.vertical or ""))
            elif isinstance(node, RangeNode):
                attrs = dict(node.attrs)
                units.append(Unit(_range_start_value(node.kind, node.token_id, attrs), None, True, path, node))
                walk(node.children, path + (node.token_id,))
                units.append(Unit(_range_end_value(node.token_id), None, True, path, node))
            elif isinstance(node, RevisionNode):
                if node.kind in ("insert", "move_to"):
                    units.append(Unit(_insert_start_value(node.kind, node.token_id), None, True, path, node))
                    walk(node.children, path + (node.token_id,))
                    units.append(Unit(_insert_end_value(node.token_id), None, True, path, node))
                else:
                    units.append(Unit(_gap_value(node.kind, node.token_id), None, True, path, node))
            else:
                attrs = dict(node.attrs)
                if isinstance(node, InlineNode) and node.style_id:
                    attrs["style"] = node.style_id
                units.append(Unit(_token_value(node.kind, node.token_id, attrs), None, True, path, node))

    walk(paragraph.nodes, ())
    return units


def flatten_edit_body(body: str) -> list[Unit]:
    """Parse an edit.md paragraph body into units (text clusters + tokens).

    Text units carry no style (assigned later); token/gap/insert units must
    match the canonical projection so the diff keeps them aligned. Insertion
    revisions are range structures in the draft: ``\u27e6insert ...\u27e7text
    \u27e6/insert id=...\u27e7`` (nestable); deletions are zero-width
    ``\u27e6revision-gap ...\u27e7`` markers, including inside insert blocks.
    """
    units: list[Unit] = []
    range_stack: list[str] = []
    insert_stack: list[tuple[str, str]] = []
    cursor = 0
    while True:
        start = body.find(TOKEN_START, cursor)
        path = tuple(range_stack + [token_id for _, token_id in insert_stack])
        if start < 0:
            tail = _validate_escaped_prose(body[cursor:])
            _append_text_units(units, tail, path)
            break
        chunk = _validate_escaped_prose(body[cursor:start])
        _append_text_units(units, chunk, path)
        end = body.find(TOKEN_END, start + 1)
        if end < 0:
            raise ValidationError("edit-grammar-invalid: unclosed placeholder")
        keyword, attrs = _parse_placeholder(body[start + 1:end])
        if keyword in ("insert", "move-to"):
            if not {"id", "kind"}.issubset(attrs) or not attrs["id"]:
                raise ValidationError(
                    "edit-grammar-invalid: insert placeholder requires id and kind"
                )
            kind = "insert" if keyword == "insert" else "move_to"
            units.append(
                Unit(_insert_start_value(kind, attrs["id"]), None, True, tuple(range_stack), None)
            )
            insert_stack.append((kind, attrs["id"]))
        elif keyword in ("/insert", "/move-to"):
            if set(attrs) != {"id"} or not attrs["id"]:
                raise ValidationError("edit-grammar-invalid: insert close requires one non-empty id")
            if not insert_stack or insert_stack[-1][1] != attrs["id"]:
                raise ValidationError("edit-grammar-invalid: mismatched or reversed insert placeholder")
            insert_stack.pop()
            units.append(Unit(_insert_end_value(attrs["id"]), None, True, tuple(range_stack), None))
        elif keyword == "token":
            if not {"id", "kind"}.issubset(attrs) or not attrs["id"] or not attrs["kind"]:
                raise ValidationError("edit-grammar-invalid: token placeholder requires id and kind")
            token_id = attrs["id"]
            kind = attrs["kind"]
            rest = {key: value for key, value in attrs.items() if key not in ("id", "kind")}
            units.append(Unit(_token_value(kind, token_id, rest), None, True, path, None))
        elif keyword == "revision-gap":
            if set(attrs) != {"id", "kind"} or not attrs["id"] or not attrs["kind"]:
                raise ValidationError(
                    "edit-grammar-invalid: revision-gap placeholder requires id and kind"
                )
            if attrs["kind"] not in ("delete", "move_from"):
                raise ValidationError(
                    f"edit-grammar-invalid: revision-gap kind must be delete or move_from: {attrs['kind']}"
                )
            units.append(Unit(_gap_value(attrs["kind"], attrs["id"]), None, True, path, None))
        elif keyword == "range-start":
            if not {"id", "kind"}.issubset(attrs) or not attrs["id"] or not attrs["kind"]:
                raise ValidationError("edit-grammar-invalid: range-start placeholder requires id and kind")
            token_id = attrs["id"]
            kind = attrs["kind"]
            rest = {key: value for key, value in attrs.items() if key not in ("id", "kind")}
            units.append(Unit(_range_start_value(kind, token_id, rest), None, True, path, None))
            range_stack.append(token_id)
        elif keyword == "range-end":
            if set(attrs) != {"id"} or not attrs["id"]:
                raise ValidationError("edit-grammar-invalid: range-end placeholder requires one non-empty id")
            if not range_stack or range_stack[-1] != attrs["id"]:
                raise ValidationError("edit-grammar-invalid: mismatched or reversed range placeholder")
            range_stack.pop()
            units.append(Unit(_range_end_value(attrs["id"]), None, True, tuple(range_stack), None))
        else:
            raise ValidationError(f"edit-grammar-invalid: unknown placeholder keyword: {keyword}")
        cursor = end + 1
    if range_stack:
        raise ValidationError("edit-grammar-invalid: unclosed range placeholder")
    if insert_stack:
        raise ValidationError("edit-grammar-invalid: unclosed insert placeholder")
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
    """Assign a style to pure-insertion text under the caret policy."""
    left = _nearest_text(base_units, i1)
    right = _next_text(base_units, i2 if i2 > i1 else i1)
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
            f"ambiguous-alignment: {paragraph_id}: the insertion point sits between two "
            f"IDENTICAL runs ({left.value!r}) carrying DIFFERENT styles, so style ownership "
            "cannot be decided without guessing — resolve it by (a) anchoring the insert "
            "to longer unique context on one side, or (b) restating the edit through "
            "document_patch with the surrounding text in the same hunk (the engine then "
            "applies proportional-preserve instead of a bare insert)"
        )
    style = left.style if left else right.style
    reason = "left-context" if left else "right-context-fallback"
    return style, reason, None


def _vertical_variant(
    style: str,
    vertical: str,
    registry: "StyleRegistry | None",
    paragraph_id: str,
) -> str:
    """One style's vertical variant, refusing to guess when unavailable."""
    if registry is None:
        raise ValidationError(
            f"vertical-tag-unavailable: {paragraph_id}: the workdir style registry is not available"
        )
    try:
        return vertical_style_variant(registry, style, vertical)
    except TypedError as exc:
        raise ValidationError(str(exc)) from exc


def _apply_vertical_styles(
    styles: list[str],
    units: list[Unit],
    registry: "StyleRegistry | None",
    paragraph_id: str,
) -> list[str]:
    """Map each tagged unit's assigned style to its vertical variant.

    ``^{…}`` / ``_{…}`` ask for a formatting the source may not carry, so the
    variant is synthesized from the style the unit was about to inherit — one
    added ``w:vertAlign`` element, registered under its canonical hash. A
    style whose own formatting already contradicts the request fails closed."""
    if not any(unit.vertical for unit in units):
        return styles
    if registry is None:
        raise ValidationError(
            f"vertical-tag-unavailable: {paragraph_id}: the workdir style registry is not available"
        )
    return [
        _vertical_variant(style, unit.vertical, registry, paragraph_id) if unit.vertical else style
        for style, unit in zip(styles, units)
    ]


def _assign_hunk_styles(
    base_units: list[Unit],
    i1: int,
    i2: int,
    current: list[Unit],
    insertion_style: str,
    paragraph_id: str,
) -> tuple[list[str], str, str | None]:
    """Assign per-unit styles to replaced text.

    Policy (frozen 2026-09-06):
    - unchanged units (equal) keep their exact baseline style;
    - a single-region rewrite inherits that region's style exactly;
    - a cross-region rewrite is styled by the explicit
      ``proportional-preserve`` policy: rewritten units map to baseline
      styles by character-position proportion across the original region
      boundaries. Deterministic and text-faithful, but semantic ownership
      of the original styles is NOT preserved - the hunk records reason
      ``proportional-preserve`` and a warning so callers can observe the
      policy. Callers wanting exact ownership must split the edit by
      style region;
    - protected range boundaries never move
      (``protected-boundary-crossing``);
    - pure insertions inside the hunk inherit the nearest handled baseline
      unit's style (caret context).
    """
    base = base_units[i1:i2]
    paths = {unit.range_path for unit in base}
    if len(paths) > 1:
        raise ValidationError(
            f"protected-boundary-crossing: {paragraph_id}: replacement spans a protected range boundary"
        )
    proportional_used = False
    matcher = SequenceMatcher(None, [u.value for u in base], [u.value for u in current], autojunk=False)
    opcodes = matcher.get_opcodes()
    full_rewrite = i1 == 0 and i2 == len(base_units)
    styles: list[str] = []
    last_style: str | None = None
    for tag, b1, b2, n1, n2 in opcodes:
        if tag == "equal":
            for offset in range(n2 - n1):
                style = base[b1 + offset].style
                styles.append(style)
                last_style = style
            continue
        if tag == "replace":
            replaced = base[b1:b2]
            replaced_styles = {unit.style for unit in replaced if not unit.token}
            if len(replaced_styles) > 1:
                proportional_used = True
                base_text = [unit for unit in replaced if not unit.token]
                base_length = sum(len(unit.value[1]) for unit in base_text)
                current_text = [unit for unit in current[n1:n2] if not unit.token]
                current_length = sum(len(unit.value[1]) for unit in current_text)
                boundaries: list[tuple[int, int, str]] = []
                cursor = 0
                for unit in base_text:
                    end = cursor + len(unit.value[1])
                    boundaries.append((cursor, end, unit.style))
                    cursor = end
                position = 0
                for unit in current[n1:n2]:
                    if unit.token or not boundaries:
                        style = last_style or insertion_style
                    else:
                        mapped = min(
                            max(0, base_length - 1),
                            position * base_length // max(1, current_length),
                        )
                        style = next(
                            style
                            for start, end, style in boundaries
                            if start <= mapped < end
                        )
                        position += len(unit.value[1])
                    styles.append(style)
                    last_style = style
                continue
            style = replaced_styles.pop() if replaced_styles else last_style
            for _ in range(n2 - n1):
                styles.append(style)
            last_style = style
            continue
        if tag == "insert":
            if last_style is None:
                style, _ = _assign_style(base_units, i1, i2, insertion_style, paragraph_id)
            else:
                style = last_style
            styles.extend([style] * (n2 - n1))
    if len(styles) != len(current):
        raise ValidationError("internal error: hunk style mapping length mismatch")
    if proportional_used:
        return styles, "proportional-preserve", (
            f"{paragraph_id}: mixed-style rewrite styled by the proportional-preserve "
            "policy (boundary-proportional assignment); semantic ownership of the "
            "original styles is not preserved"
        )
    return styles, "single-region-inheritance", None


# --------------------------------------------------------------------------
# AST rebuild
# --------------------------------------------------------------------------

def rebuild_paragraph(base_paragraph: Paragraph, units: list[Unit]) -> list[Node]:
    """Rebuild a paragraph node tree from the synced unit sequence."""
    nodes: list[Node] = []
    stack: list[Node] = []
    for unit in units:
        if not unit.token:
            target = stack[-1].children if stack else nodes
            target.append(TextNode(unit.style or base_paragraph.base_style, unit.value[1], unit.vertical or None))
            continue
        if unit.value[0] == "RS":
            node = unit.node
            if not isinstance(node, RangeNode):
                raise ValidationError("internal error: range-start unit lost its node")
            node.children = []  # rebuilt from the synced units below
            stack.append(node)
            continue
        if unit.value[0] == "IS":
            node = unit.node
            if not isinstance(node, RevisionNode):
                raise ValidationError("internal error: insert-start unit lost its node")
            node.children = []  # rebuilt from the synced units below
            stack.append(node)
            continue
        if unit.value[0] in ("RE", "IE"):
            if not stack:
                raise ValidationError("internal error: unbalanced container units")
            node = stack.pop()
            parent = stack[-1].children if stack else nodes
            parent.append(node)
            continue
        if unit.value[0] in ("G", "R"):
            if unit.node is None:
                raise ValidationError("internal error: revision unit lost its node")
            target = stack[-1].children if stack else nodes
            target.append(unit.node)
            continue
        if unit.node is None:
            raise ValidationError("internal error: token unit lost its node")
        target = stack[-1].children if stack else nodes
        target.append(unit.node)
    if stack:
        raise ValidationError("internal error: unclosed container units")
    return merge_adjacent_text(nodes)
def _token_aware_opcodes(
    baseline_values: list[tuple[Any, ...]],
    current_values: list[tuple[Any, ...]],
) -> list[tuple[str, int, int, int, int]]:
    """Diff prose between immutable tokens, keeping tokens as anchors."""
    baseline_tokens = [index for index, value in enumerate(baseline_values) if value[0] != "X"]
    current_tokens = [index for index, value in enumerate(current_values) if value[0] != "X"]
    if [baseline_values[index] for index in baseline_tokens] != [
        current_values[index] for index in current_tokens
    ]:
        return SequenceMatcher(None, baseline_values, current_values, autojunk=False).get_opcodes()
    opcodes: list[tuple[str, int, int, int, int]] = []
    base_start = current_start = 0
    for token_number in range(len(baseline_tokens) + 1):
        base_end = baseline_tokens[token_number] if token_number < len(baseline_tokens) else len(baseline_values)
        current_end = current_tokens[token_number] if token_number < len(current_tokens) else len(current_values)
        opcodes.extend(
            (
                tag,
                base_start + i1,
                base_start + i2,
                current_start + j1,
                current_start + j2,
            )
            for tag, i1, i2, j1, j2 in SequenceMatcher(
                None,
                baseline_values[base_start:base_end],
                current_values[current_start:current_end],
                autojunk=False,
            ).get_opcodes()
        )
        if token_number < len(baseline_tokens):
            opcodes.append(("equal", base_end, base_end + 1, current_end, current_end + 1))
            base_start, current_start = base_end + 1, current_end + 1
    return opcodes




# --------------------------------------------------------------------------
# Per-paragraph synchronization
# --------------------------------------------------------------------------

def _is_token_mutation(units: Iterable[Unit]) -> bool:
    return any(unit.token for unit in units)


REVISION_TAG = {"insert": "ins", "delete": "del", "move_to": "moveTo", "move_from": "moveFrom"}


def _revision_attrs(kind: str, w_id: int, author: str, date: str, date_utc: bool) -> dict[str, str]:
    attrs = {"w:id": str(w_id), "w:author": author, "w:date": date}
    if date_utc:
        attrs["w16du:dateUtc"] = date
    return attrs


def _revision_token_record(kind: str, attrs: dict[str, str]) -> dict[str, Any]:
    """Token-table record for a synthesized revision: attrs plus open/close
    XML so the renderer can emit Word-compatible markup without touching the
    template bytes."""
    tag = REVISION_TAG[kind]
    open_xml = f"<w:{tag}" + "".join(
        f' {name}="{xml_escape(str(value))}"' for name, value in attrs.items()
    ) + ">"
    return {
        "kind": "revision",
        "attrs": dict(attrs),
        "open": open_xml,
        "close": f"</w:{tag}>",
    }


def _grouped_text_nodes(units: Iterable[Unit]) -> list[TextNode]:
    """Baseline text units grouped by style and vertical alignment into
    TextNodes (a deletion may span multiple style regions; each keeps its
    original formatting)."""
    nodes: list[TextNode] = []
    for unit in units:
        vertical = unit.vertical or None
        if (
            nodes
            and nodes[-1].style_id == unit.style
            and nodes[-1].vertical == vertical
        ):
            nodes[-1] = TextNode(
                unit.style, nodes[-1].text + unit.value[1], vertical
            )
        else:
            nodes.append(TextNode(unit.style, unit.value[1], vertical))
    return nodes


def _track_hunk(
    base: list[Unit],
    current: list[Unit],
    baseline_units: list[Unit],
    i1: int,
    i2: int,
    insertion_style: str,
    paragraph_id: str,
    ctx: dict[str, Any],
    registry: "StyleRegistry | None" = None,
) -> tuple[list[Unit], list[dict[str, Any]], str | None]:
    """Wrap one text hunk in tracked revisions (ADR 0037 uniform mapping:
    insert -> ins, delete -> del, replace -> del + ins). The hunk's range path
    is preserved so edits inside an existing insertion nest inside it."""
    units_out: list[Unit] = []
    records: list[dict[str, Any]] = []
    warning: str | None = None
    base_text = "".join(unit.value[1] for unit in base)
    current_text = "".join(unit.value[1] for unit in current)
    path = (base[0].range_path if base else current[0].range_path) or ()
    ctx["revision_ids"] = ctx.get("revision_ids", set())

    def synthesize(kind: str, text_nodes: list[TextNode], text: str) -> str:
        w_id = ctx["next_w_id"]()
        token_id = ctx["next_token_id"]()
        attrs = _revision_attrs(
            kind, w_id, ctx["author"], ctx["date"], ctx.get("date_utc", False)
        )
        node = RevisionNode(token_id, kind, attrs, text_nodes)
        units_out.append(Unit(("R", kind, token_id), None, True, path, node))
        ctx["new_tokens"][token_id] = _revision_token_record(kind, attrs)
        ctx["revision_ids"].add(token_id)
        parent_key = None
        parent_path = path[:-1] if path else ()
        for token_id_on_path in reversed(path):
            parent = ctx["node_by_id"].get(token_id_on_path)
            if isinstance(parent, RevisionNode):
                parent_key = _revision_key_for_node(parent, ctx)
                break
        records.append(
            {
                "kind": kind,
                "w_id": w_id,
                "token_id": token_id,
                "text": text[:120],
                "parent_revision_key": parent_key,
            }
        )
        return token_id

    if base_text and current_text:  # replace -> delete + insert
        synthesize("delete", _grouped_text_nodes(base), base_text)
        style, reason, warning = _assign_hunk_styles(
            baseline_units, i1, i2, current, insertion_style, paragraph_id
        )
        style = _apply_vertical_styles(style, current, registry, paragraph_id)
        synthesize("insert", _insert_text_nodes(current, style), current_text)
        operation = "replace"
    elif base_text:  # pure deletion
        synthesize("delete", _grouped_text_nodes(base), base_text)
        operation = "delete"
    else:  # pure insertion
        style, reason, warning = _assign_style(
            baseline_units, i1, i2, insertion_style, paragraph_id
        )
        styles = _apply_vertical_styles([style] * len(current), current, registry, paragraph_id)
        synthesize("insert", _insert_text_nodes(current, styles), current_text)
        operation = "insert"
    return units_out, records, warning


def _insert_text_nodes(units: list[Unit], styles: list[str]) -> list[TextNode]:
    """Text nodes for one inserted run, split where the style or the vertical
    request changes (a tagged stretch keeps its own alignment)."""
    nodes: list[TextNode] = []
    for unit, style in zip(units, styles):
        text = unit.value[1]
        vertical = unit.vertical or None
        if (
            nodes
            and nodes[-1].style_id == style
            and nodes[-1].vertical == vertical
        ):
            nodes[-1] = TextNode(style, nodes[-1].text + text, vertical)
            continue
        nodes.append(TextNode(style, text, vertical))
    return nodes


def _revision_key_for_node(node: RevisionNode, ctx: dict[str, Any]) -> str:
    fingerprint = hashlib.sha256(
        f"{node.attrs}|{visible_text(node.children)}".encode("utf-8")
    ).hexdigest()[:12]
    return f"word/document.xml|{node.kind}|{node.attrs.get('w:id', '')}|{fingerprint}"

def _revision_path_in(units: Iterable[Unit], ctx: dict[str, Any]) -> bool:
    """Whether any unit sits inside a revision container (range path hits a
    revision token id) — the direct-mode mutation gate."""
    revision_ids = ctx.get("revision_ids", set())
    return any(revision_ids & set(unit.range_path) for unit in units)


def _literal_marker_count(text: str) -> int:
    return text.count("^{") + text.count("_{")


def _escaped_marker_count(body: str) -> int:
    return body.count("\\^{") + body.count("\\_{")


def _refuse_stale_vertical_markers(paragraph: Paragraph, body: str, paragraph_id: str) -> None:
    """A projection rendered before the escape existed can carry the document's
    own ``^{``/``_{`` unescaped; reading it as a tag would swallow those
    characters into formatting. Every literal marker the canonical text holds
    must therefore appear escaped in the draft — ``edit refresh`` produces
    exactly that, so the fix is one call."""
    literals = _literal_marker_count(visible_text(paragraph.nodes))
    if not literals:
        return
    if '^{' not in body and '_{' not in body:
        return
    if _escaped_marker_count(body) >= literals:
        return
    raise ValidationError(
        f"vertical-tag-ambiguous: {paragraph_id}: this paragraph's own text carries a literal "
        "'^{' or '_{' and the projection does not escape it, so a tag cannot be told apart from "
        "the text; run `edit sync`'s refresh (`edit refresh`) to re-render the projection, then redo the edit"
    )


def sync_paragraph(
    paragraph: Paragraph,
    body: str,
    insertion_style: str,
    *,
    mode: str = "direct",
    revision_ctx: dict[str, Any] | None = None,
    registry: "StyleRegistry | None" = None,
) -> tuple[list[Node], list[dict[str, Any]], list[str]]:
    """Return (new nodes, hunk records, warnings) for one edited paragraph.

    ``mode`` is the effective edit mode: ``direct`` mutates text in place,
    ``track`` wraps every text change in new insert/delete revisions,
    ``ambiguous`` rejects all text changes until the caller chooses.
    """
    _refuse_stale_vertical_markers(paragraph, body, paragraph.paragraph_id)
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
    ctx = revision_ctx or {}
    opcodes = _token_aware_opcodes(baseline_values, current_values)
    baseline_offsets = _char_offsets(baseline_units)
    current_offsets = _char_offsets(current_units)
    baseline_total = len("".join(u.value[1] for u in baseline_units if not u.token))
    current_total = len("".join(u.value[1] for u in current_units if not u.token))
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
        if mode == "ambiguous":
            raise ValidationError(
                f"edit-mode-ambiguous: {paragraph.paragraph_id}: the source has pending "
                "revisions but track changes is off (or vice versa); pass --track or "
                "--no-track to choose"
            )
        if mode == "track":
            if not ctx.get("enabled"):
                raise ValidationError(
                    "internal error: track mode requires a revision context"
                )
            tracked_units, records, warning = _track_hunk(
                base, current, baseline_units, i1, i2, insertion_style,
                paragraph.paragraph_id, ctx, registry,
            )
            output.extend(tracked_units)
            hunks.append(
                {
                    "paragraph_id": paragraph.paragraph_id,
                    "operation": "insert" if i1 == i2 else ("delete" if j1 == j2 else "replace"),
                    "baseline_range": _hunk_range(baseline_offsets, i1, i2, baseline_total),
                    "new_range": _hunk_range(current_offsets, j1, j2, current_total),
                    "source_style_set": sorted({unit.style for unit in base if unit.style}),
                    "assigned_style": None,
                    "assigned_styles": None,
                    "assignment_reason": "tracked-revision-mapping",
                    "protected_boundaries": sorted({unit.range_path for unit in base}),
                    "generated_revisions": records,
                    "warning": warning,
                }
            )
            if warning:
                warnings.append(warning)
            continue
        # direct mode: revisions are immutable here
        if _revision_path_in(base, ctx) or _revision_path_in(current, ctx):
            raise ValidationError(
                f"revision-text-mutated-in-direct-mode: {paragraph.paragraph_id}: text inside a "
                "tracked revision cannot change in direct mode; use --track to record the change"
            )
        if not any(not unit.token for unit in current):
            # pure deletion: drop the baseline text, keep everything else.
            hunks.append(
                {
                    "paragraph_id": paragraph.paragraph_id,
                    "operation": "delete",
                    "baseline_range": _hunk_range(baseline_offsets, i1, i2, baseline_total),
                    "new_range": _hunk_range(current_offsets, j1, j2, current_total),
                    "source_style_set": sorted({unit.style for unit in base if unit.style}),
                    "assigned_style": None,
                    "assignment_reason": "deletion-preserves-survivors",
                    "protected_boundaries": sorted({unit.range_path for unit in base}),
                    "warning": None,
                }
            )
            continue
        if i1 == i2:
            style, reason, warning = _assign_style(
                baseline_units, i1, i2, insertion_style, paragraph.paragraph_id
            )
            if warning:
                warnings.append(warning)
            styles = _apply_vertical_styles([style] * len(current), current, registry, paragraph.paragraph_id)
            for offset, unit in enumerate(current):
                output.append(Unit(unit.value, styles[offset], False, unit.range_path, None, unit.vertical))
        else:
            styles, reason, warning = _assign_hunk_styles(
                baseline_units, i1, i2, current, insertion_style, paragraph.paragraph_id
            )
            styles = _apply_vertical_styles(styles, current, registry, paragraph.paragraph_id)
            if warning:
                warnings.append(warning)
            for offset, unit in enumerate(current):
                output.append(Unit(unit.value, styles[offset], False, unit.range_path, None, unit.vertical))
        hunks.append(
            {
                "paragraph_id": paragraph.paragraph_id,
                "operation": "insert" if i1 == i2 else "replace",
                "baseline_range": _hunk_range(baseline_offsets, i1, i2, baseline_total),
                "new_range": _hunk_range(current_offsets, j1, j2, current_total),
                "source_style_set": sorted({unit.style for unit in base if unit.style}),
                "assigned_style": styles[0] if styles else None,
                "assigned_styles": styles,
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



def pending_insertion_author(paragraph: Paragraph) -> str | None:
    """Author of the paragraph-mark insertion that makes this paragraph new.

    ``None`` means "not a pending insertion". A paragraph created by an insert
    carries its insertion on the paragraph *mark* (``pPr/rPr/w:ins``); its text
    is ordinary runs — which is why editing inside it is an in-place rewrite and
    not a nested revision. ``""`` means the mark records no author.
    """
    mark = paragraph.mark_revision or {}
    if mark.get("kind") != "insert":
        return None
    author = (mark.get("attrs") or {}).get("w:author")
    return str(author) if author else ""


def pending_insertion_action(paragraph: Paragraph, author: str) -> str:
    """What this author may do with the paragraph's pending insertion.

    ``"none"``   — not a pending insertion; ordinary editing rules apply.
    ``"absorb"`` — rewrite the insertion in place (its own author, or the mark
                   records no author): the insertion stays one insertion.
    ``"refuse"`` — another author's edit would have to be recorded *inside*
                   someone else's insertion. Word represents that by splitting
                   the insertion at the edit point; generating that shape is
                   not implemented, so it is refused rather than nested.
    """
    if not paragraph.inherit:
        return "none"
    owner = pending_insertion_author(paragraph)
    if owner is None or not owner or owner == author:
        return "absorb"
    return "refuse"


def _refuse_foreign_insertion(paragraph: Paragraph, author: str) -> ValidationError:
    owner = pending_insertion_author(paragraph)
    return ValidationError(
        f"edit-inside-pending-insertion: {paragraph.paragraph_id} is a pending insertion by "
        f"{owner!r}; an edit by {author!r} would have to be recorded as a revision inside it, "
        "which this engine does not generate — accept that insertion revision first, or make "
        "the change in Word"
    )


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
    new_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    generated_revisions: list[dict[str, Any]] = field(default_factory=list)
    styles: dict[str, Any] | None = None  # set only when the sync added a variant


def plan_sync(
    typed: TypedDocument,
    projection: Any,
    format_data: dict[str, Any],
    *,
    mode: str | None = None,
    revision_ctx: dict[str, Any] | None = None,
    styles: dict[str, Any] | None = None,
    edited_ids: Collection[str] | None = None,
) -> SyncPlan:
    """Build the synced typed document from the edited projection.

    ``mode`` defaults to the three-field state from the source signals
    (``effective_edit_mode``). ``revision_ctx`` supplies identity and token
    allocation for tracked edits (ADR 0037). ``edited_ids`` is the set of
    paragraphs the caller actually changed: the pending-insertion policy
    (absorption vs refusal, ADR 0047) applies to those only — a paragraph the
    caller merely re-stated must not decide the fate of the whole patch.
    ``None`` means "unknown", and the policy then stays out of the way (the
    post-plan check still refuses a nested shape). Raises ValidationError with
    stable diagnostics on any policy violation; never mutates files.
    """
    from .typed_core import effective_edit_mode, promote_vertical_alignment

    records = {record["id"]: record for record in format_data.get("paragraphs", [])}
    by_id = {paragraph.paragraph_id: paragraph for paragraph in typed.paragraphs}
    used_ids = set(by_id) | set(typed.deletions)
    ctx = revision_ctx or {}
    if mode is None:
        mode = effective_edit_mode(
            source_track_enabled=bool(format_data.get("source_track_enabled")),
            has_pending_revisions=_document_has_revisions(typed),
        )
    ctx["mode"] = mode
    plan = SyncPlan(TypedDocument(dict(typed.meta)))
    registry = StyleRegistry.from_json(styles) if isinstance(styles, dict) else None
    styles_before = set((styles or {}).get("styles") or {})
    for kind, attrs, body in projection.paragraphs:
        if kind == "new":
            if mode == "track":
                pass  # paragraph-mark revisions apply to body paragraphs
            marker_inherit = attrs["inherit"]
            # The anchor may be a paragraph an earlier commit created, and
            # that paragraph's own ``inherit`` names the source paragraph it
            # copies. Follow the chain: validation refuses a new paragraph
            # whose inherit is not a paragraph the template baseline carries,
            # so a chained insert must bind to the source, not to the anchor.
            inherit = marker_inherit
            seen: set[str] = set()
            while inherit not in records and inherit in by_id and inherit not in seen:
                seen.add(inherit)
                inherit = by_id[inherit].inherit or ""
            if inherit not in records:
                raise ValidationError(f"unknown inherit paragraph in @new marker: {marker_inherit}")
            if _body_has_tokens(body):
                raise ValidationError(
                    f"new paragraph cannot contain structural tokens: {attrs['temp']}"
                )
            if inherit.startswith(("T", "B")) or ("." in inherit and not inherit.startswith("P")):
                raise ValidationError(
                    f"table-structure-immutable: new paragraphs cannot be inserted "
                    f"into tables, text boxes, or header/footer/note parts "
                    f"(inherit {inherit}); container structure operations are out of scope"
                )
            new_id = _next_paragraph_id(used_ids)
            used_ids.add(new_id)
            insertion_style = records[inherit].get("insertion_style") or records[inherit].get("base_style", "")
            text = _validate_escaped_prose(body)
            new_nodes: list[Node] = [
                TextNode(
                    _vertical_variant(insertion_style, vertical, registry, new_id) if vertical else insertion_style,
                    prose,
                )
                for prose, vertical in _split_vertical_tags(text)
            ]
            mark_revision = None
            if mode == "track":
                mark_revision = _synthesize_paragraph_mark("insert", ctx)
            plan.document.paragraphs.append(
                Paragraph(
                    new_id,
                    records[inherit].get("base_style", ""),
                    merge_adjacent_text(new_nodes),
                    inherit=inherit,
                    mark_revision=mark_revision,
                )
            )
            if mark_revision is not None:
                plan.hunks.append(
                    {
                        "paragraph_id": new_id,
                        "operation": "paragraph-mark-insert",
                        "baseline_range": [0, 0],
                        "new_range": [0, 0],
                        "source_style_set": [],
                        "assigned_style": None,
                        "assigned_styles": None,
                        "assignment_reason": "paragraph-mark-revision",
                        "protected_boundaries": [],
                        "generated_revisions": [
                            {
                                "kind": "insert",
                                "w_id": int(mark_revision["attrs"].get("w:id", -1)),
                                "token_id": mark_revision["token_id"],
                                "text": "",
                                "parent_revision_key": None,
                                "scope": "paragraph-mark",
                            }
                        ],
                        "warning": None,
                    }
                )
            plan.new_ids.append(new_id)
            plan.changed_ids.append(new_id)
            continue
        paragraph_id = attrs["id"]
        paragraph = by_id[paragraph_id]
        record = records.get(paragraph_id)
        insertion_style = (record or {}).get("insertion_style") or paragraph.base_style
        author_now = (ctx or {}).get("author", "") if ctx else ""
        in_scope = edited_ids is None or paragraph.paragraph_id in edited_ids
        insertion_action = (
            pending_insertion_action(paragraph, author_now)
            if (author_now and in_scope)
            else "none"
        )
        if insertion_action == "refuse":
            raise _refuse_foreign_insertion(paragraph, author_now)
        # a pending insertion absorbs the edit: the paragraph is already the
        # tracked change, so its content is rewritten in place instead of
        # gaining a nested revision (Word shows the same thing when the author
        # keeps editing what they just inserted)
        absorbed = insertion_action == "absorb"
        nodes, hunks, warnings = sync_paragraph(
            paragraph, body, insertion_style,
            mode="direct" if absorbed else mode,
            revision_ctx=None if absorbed else ctx,
            registry=registry,
        )
        if absorbed:
            for hunk in hunks:
                hunk["absorbed"] = "pending-insertion"
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
            original_index=(record or {}).get("original_index", -1),
            mark_revision=paragraph.mark_revision,
            part_key=paragraph.part_key,
            part_entry_id=paragraph.part_entry_id,
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
        if paragraph.inherit:
            # undoing a pending insertion: the paragraph carries its own
            # w:ins on the paragraph mark, so there is nothing in the baseline
            # to delete — it disappears, mark and all (Word does the same when
            # an author deletes a paragraph they just inserted)
            delete_author = (ctx or {}).get("author", "") if ctx else ""
            if delete_author and pending_insertion_action(paragraph, delete_author) == "refuse":
                raise _refuse_foreign_insertion(paragraph, delete_author)
            plan.document.paragraphs = [
                existing for existing in plan.document.paragraphs
                if existing.paragraph_id != paragraph_id
            ]
            plan.deleted_ids.append(paragraph_id)
            continue
        if paragraph_id.startswith(("T", "B")) or ("." in paragraph_id and not paragraph_id.startswith("P")):
            raise ValidationError(
                f"table-structure-immutable: container and part paragraphs cannot "
                f"be deleted ({paragraph_id}); container structure operations are "
                "out of scope"
            )
        if paragraph.section_bearing or any(
            not isinstance(node, TextNode)
            for node in _iter_nodes(paragraph.nodes)
        ):
            raise ValidationError(
                f"paragraph with protected structure cannot be deleted: {paragraph_id}"
            )
        record = records.get(paragraph_id) or {}
        if mode == "track":
            # Paragraph-mark deletion (R2.5): the paragraph stays in the
            # document and its pilcrow revision is recorded; Word displays the
            # paragraph merged into its predecessor (merge semantics are a
            # project definition, not a Word fidelity claim).
            mark = _synthesize_paragraph_mark("delete", ctx)
            tracked = Paragraph(
                paragraph.paragraph_id,
                paragraph.base_style,
                paragraph.nodes,
                p_open=paragraph.p_open,
                ppr=paragraph.ppr,
                raw_xml=paragraph.raw_xml,
                section_bearing=paragraph.section_bearing,
                editable=paragraph.editable,
                inherit=paragraph.inherit,
                original_index=record.get("original_index", -1),
                mark_revision=mark,
            )
            insert_at = len(plan.document.paragraphs)
            original_index = record.get("original_index", -1)
            for position, existing in enumerate(plan.document.paragraphs):
                if existing.original_index > original_index:
                    insert_at = position
                    break
            plan.document.paragraphs.insert(insert_at, tracked)
            plan.hunks.append(
                {
                    "paragraph_id": paragraph.paragraph_id,
                    "operation": "paragraph-mark-delete",
                    "baseline_range": [0, 0],
                    "new_range": [0, 0],
                    "source_style_set": [],
                    "assigned_style": None,
                    "assigned_styles": None,
                    "assignment_reason": "paragraph-mark-revision",
                    "protected_boundaries": [],
                    "generated_revisions": [
                        {
                            "kind": "delete",
                            "w_id": int(mark["attrs"].get("w:id", -1)),
                            "token_id": mark["token_id"],
                            "text": "",
                            "parent_revision_key": None,
                            "scope": "paragraph-mark",
                        }
                    ],
                    "warning": None,
                }
            )
            plan.changed_ids.append(paragraph_id)
            continue
        plan.document.deletions.append(paragraph_id)
        plan.deleted_ids.append(paragraph_id)
        plan.changed_ids.append(paragraph_id)
    if registry is not None and str(typed.meta.get("schema", "1")) == "2":
        promote_vertical_alignment(plan.document, registry)
    for hunk in plan.hunks:
        plan.generated_revisions.extend(hunk.get("generated_revisions", []))
    plan.new_tokens.update(ctx.get("new_tokens", {}))
    if registry is not None and set(registry.styles) != styles_before:
        plan.styles = registry.to_json()
    return plan


def _synthesize_paragraph_mark(kind: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Allocate a paragraph-mark revision (self-closing w:ins/w:del inside
    pPr/rPr) with session identity and record its token XML."""
    tag = {"insert": "ins", "delete": "del"}[kind]
    w_id = ctx["next_w_id"]()
    token_id = ctx["next_token_id"]()
    attrs = _revision_attrs(kind, w_id, ctx["author"], ctx["date"], ctx.get("date_utc", False))
    open_xml = f"<w:{tag}" + "".join(
        f' {name}="{xml_escape(str(value))}"' for name, value in attrs.items()
    ) + "/>"
    ctx["new_tokens"][token_id] = {
        "kind": "paragraph-mark",
        "attrs": dict(attrs),
        "raw": open_xml,
    }
    ctx.setdefault("revision_ids", set()).add(token_id)
    return {"kind": kind, "token_id": token_id, "attrs": attrs}


def _document_has_revisions(document: TypedDocument) -> bool:
    """Whether the typed document carries any tracked revision (run-level
    nodes or paragraph marks)."""
    return (
        any(
            isinstance(node, RevisionNode)
            for node in _iter_nodes(
                node for paragraph in document.paragraphs for node in paragraph.nodes
            )
        )
        or any(paragraph.mark_revision for paragraph in document.paragraphs)
    )


def _iter_nodes(nodes: Iterable[Node]) -> Iterable[Node]:
    for node in nodes:
        yield node
        if isinstance(node, (RangeNode, RevisionNode)):
            yield from _iter_nodes(node.children)


# --------------------------------------------------------------------------
# Region view (regions.md)
# --------------------------------------------------------------------------

def render_regions_md(document: TypedDocument, styles: StyleRegistry) -> str:
    """Render the read-only style-region view of a document.

    Regions are maximal runs of equal style and vertical alignment. Tokens
    appear as unnumbered markers between editable text regions.
    """
    lines = [
        "# Style regions",
        "",
        "Each region is [index] text {style_id: description; vertical=...}. "
        "Equal style_id and vertical alignment mean identical formatting.",
        "Different style_id or vertical alignment means different formatting, "
        "even if descriptions match.",
        "Tokens (tabs, breaks, hyperlinks, opaque nodes) appear as unnumbered "
        "markers and are not editable as text. Dictionary: docs/rpr-reference.md",
        "",
    ]
    for paragraph in document.paragraphs:
        header = f"## {paragraph.paragraph_id}"
        if paragraph.inherit:
            header += f" (inherit {paragraph.inherit})"
        lines.append(header)
        units = flatten_paragraph(paragraph)
        regions: list[tuple[str, str | None, str | None]] = []
        for unit in units:
            if unit.token:
                if unit.value[0] == "G":
                    marker = "\u27e6revision-gap\u27e7"
                elif unit.value[0] == "IS":
                    marker = f"\u27e6insert {unit.value[1]}\u27e7"
                elif unit.value[0] == "IE":
                    marker = "\u27e6/insert\u27e7"
                else:
                    marker = "\u27e6token\u27e7"
                regions.append((marker, None, None))
                continue
            vertical = unit.vertical or None
            if regions and regions[-1][1:] == (unit.style, vertical):
                regions[-1] = (regions[-1][0] + unit.value[1], unit.style, vertical)
            else:
                regions.append((unit.value[1], unit.style, vertical))
        index = 0
        for text, style, vertical in regions:
            if style is None:
                lines.append(f"  {text}")
                continue
            style_obj = styles.styles.get(style)
            description = style_obj.label if style_obj else style
            suffix = f"; vertical={vertical}" if vertical else ""
            lines.append(f"[{index}] {text} {{s_{style[2:10]}: {description}{suffix}}}")
            index += 1
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Revision inventory (revisions.json / revisions.md)
# --------------------------------------------------------------------------

def collect_document_revisions(document: TypedDocument) -> list[dict[str, Any]]:
    """Direct-body revisions from the typed AST (editable surface), plus
    paragraph-mark revisions (viewable, not independently editable)."""
    revisions: list[dict[str, Any]] = []
    for paragraph in document.paragraphs:
        editable = not contains_opaque(paragraph.nodes)
        for node in _walk_revisions(paragraph.nodes):
            text = "".join(
                child.text for child in node.children if isinstance(child, TextNode)
            )
            attrs = node.attrs
            revisions.append(
                {
                    "paragraph_id": paragraph.paragraph_id,
                    "kind": node.kind,
                    "w_id": attrs.get("w:id", ""),
                    "author": attrs.get("w:author", ""),
                    "date": attrs.get("w:date", ""),
                    "text": text,
                    "editable": editable,
                    "reason": None if editable else "paragraph-contains-unsupported-node",
                }
            )
        mark = paragraph.mark_revision
        if mark is not None:
            attrs = mark.get("attrs", {})
            revisions.append(
                {
                    "paragraph_id": paragraph.paragraph_id,
                    "kind": mark["kind"],
                    "w_id": attrs.get("w:id", ""),
                    "author": attrs.get("w:author", ""),
                    "date": attrs.get("w:date", ""),
                    "text": "",
                    "editable": False,
                    "reason": "paragraph-mark-revision",
                    "scope": "paragraph-mark",
                }
            )
    return revisions


def _walk_revisions(nodes: Iterable[Node]) -> Iterable[RevisionNode]:
    for node in nodes:
        if isinstance(node, RevisionNode):
            yield node
            yield from _walk_revisions(node.children)
        elif isinstance(node, (RangeNode, TextNode)):
            if isinstance(node, RangeNode):
                yield from _walk_revisions(node.children)


def _revision_key(entry: dict[str, Any]) -> str:
    import hashlib

    digest = hashlib.sha256(entry.get("text", "").encode("utf-8")).hexdigest()[:12]
    return f"{entry.get('part', 'document.xml')}|{entry['kind']}|{entry.get('w_id', '?')}|{digest}"


def render_revisions_json(
    document: TypedDocument,
    package_revisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full revision inventory: document.xml direct-body revisions plus the
    package-wide read-only scan."""
    document_entries = collect_document_revisions(document)
    for entry in document_entries:
        entry["part"] = "word/document.xml"
    package_entries = [
        entry
        for entry in (package_revisions or [])
        if entry.get("part") != "word/document.xml"
    ]
    entries = document_entries + [
        {**entry, "editable": False, "reason": "nested-container-or-non-editable-part"}
        for entry in package_entries
    ]
    for entry in entries:
        entry["revision_key"] = _revision_key(entry)
    return {"schema": "typed-revisions-1", "revisions": entries}


def render_revisions_md(inventory: dict[str, Any]) -> str:
    lines = [
        "# Revisions",
        "",
        "Tracked changes in this document. editable=false entries live outside",
        "the editable surface (nested containers, other parts) and can only be",
        "viewed; direct-body revisions can be accepted/rejected in R3.",
        "",
    ]
    for entry in inventory["revisions"]:
        flag = "ok" if entry.get("editable") else f"locked ({entry.get('reason')})"
        lines.append(
            f"- {entry['kind']} w:id={entry.get('w_id', '?')} "
            f"[{entry.get('paragraph_id', entry.get('part', '?'))}] "
            f"@{entry.get('author', '?')} {entry.get('date', '')} "
            f"{entry.get('text', '')[:40]!r} — {flag}"
        )
    return "\n".join(lines) + "\n"

# --------------------------------------------------------------------------
# Sync evidence helpers
# --------------------------------------------------------------------------

def sync_segments_from_nodes(nodes: list[Node]) -> list[list[str | None]]:
    """Return governed segments as ``[style, text, vertical]`` records."""
    return [
        [style, text, vertical]
        for text, style, vertical in _text_segment_pairs(nodes)
    ]


def _text_segment_pairs(
    nodes: Iterable[Node],
) -> list[tuple[str, str, str | None]]:
    pairs: list[tuple[str, str, str | None]] = []
    for node in nodes:
        if isinstance(node, TextNode):
            if node.text:
                pairs.append((node.text, node.style_id, node.vertical or None))
        elif isinstance(node, RangeNode):
            pairs.extend(_text_segment_pairs(node.children))
        elif isinstance(node, RevisionNode):
            if node.kind in ("insert", "move_to"):
                pairs.extend(_text_segment_pairs(node.children))  # final view
    return pairs
