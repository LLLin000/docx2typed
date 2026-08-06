"""DOCX extraction, typed workdirs, byte patching, and independent verify."""
from __future__ import annotations

from dataclasses import dataclass
import argparse
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Iterable

try:
    from .typed_core import (
        NS_R,
        NS_W,
        AnchorNode,
        InlineNode,
        OpaqueNode,
        Paragraph,
        RangeNode,
        RevisionNode,
        StyleRegistry,
        TextNode,
        TypedDocument,
        TypedError,
        choose_base_style,
        canonical_xml,
        content_signature,
        contains_opaque,
        element_end_xml,
        element_start_xml,
        etree_xml,
        local_name,
        merge_adjacent_text,
        parse_typed,
        qname,
        serialize_typed,
        skeleton,
        style_id_for_rpr,
        visible_text,
        visible_text_original,
        w,
        xml_escape,
    )
except ImportError:
    from typed_core import (
        NS_R,
        NS_W,
        AnchorNode,
        InlineNode,
        OpaqueNode,
        Paragraph,
        RangeNode,
        RevisionNode,
        StyleRegistry,
        TextNode,
        TypedDocument,
        TypedError,
        choose_base_style,
        canonical_xml,
        content_signature,
        contains_opaque,
        element_end_xml,
        element_start_xml,
        etree_xml,
        local_name,
        merge_adjacent_text,
        parse_typed,
        qname,
        serialize_typed,
        skeleton,
        style_id_for_rpr,
        visible_text,
        visible_text_original,
        w,
        xml_escape,
    )


class ValidationError(TypedError):
    """A workdir cannot be safely built."""


@dataclass
class ParagraphSlice:
    index: int
    start: int
    end: int
    raw: bytes


@dataclass
class DocumentSlices:
    xml: bytes
    body_start: int
    body_end: int
    paragraphs: list[ParagraphSlice]


@dataclass
class ParsedDocx:
    document: TypedDocument
    styles: StyleRegistry
    tokens: dict[str, dict[str, Any]]
    slices: DocumentSlices


@dataclass
class ValidatedWorkdir:
    path: Path
    format_data: dict[str, Any]
    styles: StyleRegistry
    typed: TypedDocument
    baseline: TypedDocument
    baseline_tokens: dict[str, dict[str, Any]]
    template_path: Path
    template_xml: bytes
    template_slices: DocumentSlices
    live_paragraphs: list[Paragraph]
    baseline_by_id: dict[str, Paragraph]
    warnings: list[str]


_TAG_RE = re.compile(rb"<!--.*?-->|<[^>]+>", re.DOTALL)
_START_TAG_RE = re.compile(rb"<\s*([A-Za-z_][A-Za-z0-9_.:-]*)(?:\s[^>]*?)?/?>")
_CLOSE_TAG_RE = re.compile(rb"</\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*>")
_P_OPEN_RE = re.compile(r"^<(?:[A-Za-z_][\w.-]*:)?p(?:\s[^>]*?)?/?>")
_PPR_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?pPr(?:\s[^>]*)?>.*?</(?:[A-Za-z_][\w.-]*:)?pPr>", re.DOTALL)
_P_ATTR_RE = re.compile(
    r'\s+(?P<name>[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?)\s*=\s*(?P<value>"[^"]*"|\'[^\']*\')'
)
_EPHEMERAL_P_ATTRS = {
    "paraId",
    "textId",
    "rsidP",
    "rsidR",
    "rsidRDefault",
    "rsidDel",
    "rsidRPr",
}




def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())
def json_bytes(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def zip_manifest(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: sha256_bytes(archive.read(name)) for name in sorted(archive.namelist())}


def _tag_name(token: bytes) -> tuple[str, bool, bool] | None:
    if token.startswith(b"<!--") or token.startswith(b"<?") or token.startswith(b"<!["):
        return None
    closing = bool(re.match(rb"<\s*/", token))
    if closing:
        match = _CLOSE_TAG_RE.fullmatch(token)
        return (match.group(1).decode("ascii"), True, False) if match else None
    match = _START_TAG_RE.fullmatch(token)
    if not match:
        return None
    return (match.group(1).decode("ascii"), False, token.rstrip().endswith(b"/>"))


def locate_document_xml(xml: bytes) -> DocumentSlices:
    """Locate only direct ``w:body`` paragraphs without rewriting XML."""
    stack: list[tuple[str, int]] = []
    body_depth: int | None = None
    body_start = -1
    body_end = -1
    paragraphs: list[ParagraphSlice] = []
    paragraph_starts: list[tuple[int, int]] = []

    for match in _TAG_RE.finditer(xml):
        token = match.group(0)
        parsed = _tag_name(token)
        if parsed is None:
            continue
        raw_name, closing, self_closing = parsed
        name = raw_name.rsplit(":", 1)[-1]
        if closing:
            if not stack or stack[-1][0] != name:
                raise ValidationError(f"malformed document XML nesting near {raw_name}")
            if name == "p" and body_depth is not None and len(stack) == body_depth + 2:
                start, index = stack[-1][1], len(paragraph_starts)
                paragraph_starts.append((start, match.end()))
            if name == "body" and body_depth is not None and len(stack) == body_depth + 1:
                body_end = match.start()
            stack.pop()
            continue
        depth = len(stack)
        if name == "body" and body_depth is None:
            body_depth = depth
            body_start = match.end()
        if body_depth is not None and name == "p" and depth == body_depth + 1:
            if self_closing:
                paragraph_starts.append((match.start(), match.end()))
            else:
                stack.append((name, match.start()))
        elif not self_closing:
            stack.append((name, match.start()))
        if self_closing and name == "body":
            body_end = match.start()
    if stack:
        raise ValidationError("document XML has unclosed elements")
    if body_start < 0 or body_end < body_start:
        raise ValidationError("document XML has no direct w:body")
    for index, (start, end) in enumerate(paragraph_starts):
        paragraphs.append(ParagraphSlice(index, start, end, xml[start:end]))
    return DocumentSlices(xml, body_start, body_end, paragraphs)


def _raw_p_parts(raw: bytes) -> tuple[str, str]:
    text = raw.decode("utf-8")
    opening = _P_OPEN_RE.match(text)
    if not opening:
        raise ValidationError("direct paragraph has no recognizable w:p opening")
    p_open = opening.group(0)
    if p_open.endswith("/>"):
        # A touched paragraph must render as a paired element, never self-closing.
        p_open = p_open[:-2] + ">"
    ppr_match = _PPR_RE.search(text)
    return p_open, ppr_match.group(0) if ppr_match else ""


def _attrs(element: ET.Element) -> dict[str, str]:
    return {qname(key): value for key, value in element.attrib.items()}


def _token_ids(nodes: Iterable[Any]) -> list[list[str]]:
    values: list[list[str]] = []
    for node in nodes:
        if isinstance(node, (RangeNode, RevisionNode)):
            values.append([node.token_id, node.kind])
            values.extend(_token_ids(node.children))
        elif isinstance(node, (AnchorNode, InlineNode, OpaqueNode)):
            values.append([node.token_id, node.kind])
    return values


def _assign_default_style(nodes: Iterable[Any], style_id: str) -> None:
    for node in nodes:
        if isinstance(node, TextNode) and not node.style_id:
            node.style_id = style_id
        elif isinstance(node, (RangeNode, RevisionNode)):
            _assign_default_style(node.children, style_id)


def _contains_structural(nodes: Iterable[Any]) -> bool:
    return any(not isinstance(node, TextNode) for node in nodes)


def _iter_anchor_nodes(nodes: Iterable[Any]) -> Iterable[AnchorNode]:
    for node in nodes:
        if isinstance(node, AnchorNode):
            yield node
        elif isinstance(node, (RangeNode, RevisionNode)):
            yield from _iter_anchor_nodes(node.children)


def _validate_anchor_pairs(paragraphs: Iterable[Paragraph], scope: str) -> None:
    pairs = {
        "bookmark": ("bookmark-start", "bookmark-end"),
        "comment": ("comment-start", "comment-end"),
    }
    positions: dict[str, dict[str, list[tuple[str, int]]]] = {
        kind: {"start": [], "end": []} for kind in pairs
    }
    for paragraph_index, paragraph in enumerate(paragraphs):
        for node in _iter_anchor_nodes(paragraph.nodes):
            for family, (start_kind, end_kind) in pairs.items():
                if node.kind == start_kind:
                    anchor_id = node.attrs.get("w:id") or node.attrs.get("id")
                    if not anchor_id:
                        raise ValidationError(f"{scope} {family} start has no ID")
                    positions[family]["start"].append((anchor_id, paragraph_index))
                elif node.kind == end_kind:
                    anchor_id = node.attrs.get("w:id") or node.attrs.get("id")
                    if not anchor_id:
                        raise ValidationError(f"{scope} {family} end has no ID")
                    positions[family]["end"].append((anchor_id, paragraph_index))
    for family, sides in positions.items():
        starts = sides["start"]
        ends = sides["end"]
        start_ids = [anchor_id for anchor_id, _ in starts]
        end_ids = [anchor_id for anchor_id, _ in ends]
        if sorted(start_ids) != sorted(end_ids):
            raise ValidationError(f"{scope} {family} anchors are not paired")
        if len(start_ids) != len(set(start_ids)):
            raise ValidationError(f"{scope} {family} anchor IDs are duplicated")
        for anchor_id, start_paragraph in starts:
            end_paragraph = next(index for candidate, index in ends if candidate == anchor_id)
            if start_paragraph > end_paragraph:
                raise ValidationError(f"{scope} {family} anchor range is reversed: {anchor_id}")


class _TokenTable:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.next_id = 0

    def add(self, kind: str, **record: Any) -> str:
        token_id = f"N{self.next_id}"
        self.next_id += 1
        self.records[token_id] = {"kind": kind, **record}
        return token_id


def _empty_rpr() -> str:
    return f'<w:rPr xmlns:w="{NS_W}"/>'


def _parse_run(element: ET.Element, styles: StyleRegistry, tokens: _TokenTable) -> list[Any]:
    rpr = next((child for child in element if local_name(child.tag) == "rPr"), None)
    rpr_xml = etree_xml(rpr) if rpr is not None else _empty_rpr()
    style_id = styles.ensure(rpr_xml)
    output: list[Any] = []
    known_inline = {"t", "tab", "br", "cr", "noBreakHyphen", "softHyphen", "sym", "commentReference"}
    format_change = None
    if rpr is not None:
        for child in rpr:
            if local_name(child.tag) == "rPrChange":
                format_change = child
                break
    for child in element:
        name = local_name(child.tag)
        if name == "rPr":
            continue
        if name == "t" or name == "delText":
            output.append(TextNode(style_id, child.text or ""))
        elif name in known_inline:
            token_id = tokens.add(
                name if name != "cr" else "cr",
                raw=etree_xml(child),
                attrs=_attrs(child),
                style_id=style_id,
            )
            output.append(InlineNode(token_id, name if name != "cr" else "cr", style_id, _attrs(child)))
        else:
            token_id = tokens.add(
                "unsupported-run",
                raw=etree_xml(element),
                attrs={"tag": qname(child.tag)},
                style_id=style_id,
            )
            return [OpaqueNode(token_id, "unsupported-run", {"tag": qname(child.tag)})]
    if format_change is not None:
        token_id = tokens.add("rpr-change", raw=etree_xml(format_change), attrs={"tag": "w:rPrChange"})
        output.append(OpaqueNode(token_id, "rpr-change", {"tag": "w:rPrChange"}))
    if not output:
        token_id = tokens.add("empty-run", raw=etree_xml(element), attrs={}, style_id=style_id)
        return [InlineNode(token_id, "empty-run", style_id, {})]
    return merge_adjacent_text(output)


def _parse_container(children: Iterable[ET.Element], styles: StyleRegistry, tokens: _TokenTable) -> list[Any]:
    output: list[Any] = []
    for element in children:
        name = local_name(element.tag)
        if name in {"pPr", "proofErr"}:
            if name == "proofErr":
                token_id = tokens.add("opaque", raw=etree_xml(element), attrs={"tag": qname(element.tag)})
                output.append(OpaqueNode(token_id, "opaque", {"tag": qname(element.tag)}))
            continue
        if name == "r":
            output.extend(_parse_run(element, styles, tokens))
            continue
        if name == "hyperlink":
            token_id = tokens.add(
                "hyperlink",
                open=element_start_xml(element),
                close=element_end_xml(element),
                attrs=_attrs(element),
            )
            output.append(RangeNode(token_id, "hyperlink", _attrs(element), _parse_container(list(element), styles, tokens)))
            continue
        if name in {"ins", "del", "moveFrom", "moveTo"}:
            kind = {
                "ins": "insert",
                "del": "delete",
                "moveFrom": "move_from",
                "moveTo": "move_to",
            }[name]
            token_id = tokens.add(
                "revision",
                open=element_start_xml(element),
                close=element_end_xml(element),
                attrs=_attrs(element),
            )
            output.append(
                RevisionNode(
                    token_id,
                    kind,
                    _attrs(element),
                    _parse_container(list(element), styles, tokens),
                )
            )
            continue
        if name in {"bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd"}:
            kind = {
                "bookmarkStart": "bookmark-start",
                "bookmarkEnd": "bookmark-end",
                "commentRangeStart": "comment-start",
                "commentRangeEnd": "comment-end",
            }[name]
            attrs = _attrs(element)
            token_id = tokens.add(kind, raw=etree_xml(element), attrs=attrs)
            output.append(AnchorNode(token_id, kind, attrs))
            continue
        token_id = tokens.add("opaque", raw=etree_xml(element), attrs={"tag": qname(element.tag)})
        output.append(OpaqueNode(token_id, "opaque", {"tag": qname(element.tag)}))
    return merge_adjacent_text(output)


def _parse_paragraph(element: ET.Element, raw: bytes, index: int, styles: StyleRegistry, tokens: _TokenTable) -> Paragraph:
    p_open, ppr_xml = _raw_p_parts(raw)
    children = _parse_container(list(element), styles, tokens)
    section_bearing = "sectPr" in ppr_xml
    mark_revision = _parse_mark_revision(ppr_xml, tokens)
    paragraph = Paragraph(
        paragraph_id=f"P{index}",
        base_style="",
        nodes=children,
        p_open=p_open,
        ppr=ppr_xml,
        raw_xml=raw.decode("utf-8"),
        section_bearing=section_bearing,
        editable=not contains_opaque(children),
        original_index=index,
        mark_revision=mark_revision,
    )
    return paragraph


_MARK_TAG_RE = re.compile(
    r"<w:(ins|del)(?:\s+[^>]*)?/>"
)


def _parse_mark_revision(ppr_xml: str, tokens: _TokenTable) -> dict[str, Any] | None:
    """Paragraph-mark revision: a self-closing w:ins/w:del inside pPr/rPr.

    Only the mark itself is recorded; the surrounding rPr stays with the
    template bytes. Returns {"kind", "token_id", "attrs"} or None.
    """
    if not ppr_xml:
        return None
    match = _MARK_TAG_RE.search(ppr_xml)
    if match is None:
        return None
    kind = {"ins": "insert", "del": "delete"}[match.group(1)]
    attrs = _parse_attrs_xml(match.group(0))
    token_id = tokens.add(
        "paragraph-mark",
        raw=match.group(0),
        attrs=attrs,
    )
    return {"kind": kind, "token_id": token_id, "attrs": attrs}


def _parse_attrs_xml(tag_xml: str) -> dict[str, str]:
    """Attribute map of a self-contained XML tag.

    The fragment carries no xmlns declarations, so it is parsed inside a
    wrapper that declares every python-docx namespace prefix (plus w16du,
    which python-docx does not ship); attribute names are normalized to
    qname form.
    """
    from docx.oxml.ns import nsmap
    from .typed_core import NS_W16DU

    declarations = " ".join(
        f'xmlns:{prefix}="{uri}"'
        for prefix, uri in {**nsmap, "w16du": NS_W16DU}.items()
    )
    wrapper = f"<docx2typed-root {declarations}>{tag_xml}</docx2typed-root>"
    root = ET.fromstring(wrapper)
    return {qname(key): value for key, value in root[0].attrib.items()}


def _inject_mark_revision(ppr: str, mark: dict[str, Any], tokens: dict[str, dict[str, Any]]) -> str:
    """Inject the paragraph-mark revision XML into pPr's rPr (Word places the
    pilcrow revision on the paragraph mark run properties)."""
    record = tokens.get(mark["token_id"])
    mark_xml = str(record.get("raw", "")) if record and record.get("raw") else ""
    if not mark_xml:
        raise ValidationError(f"missing paragraph-mark XML: {mark['token_id']}")
    rpr_match = re.search(r"<w:rPr(?:\s+[^>]*)?>", ppr)
    if rpr_match:
        return ppr[: rpr_match.end()] + mark_xml + ppr[rpr_match.end():]
    sect_match = re.search(r"<w:sectPr", ppr)
    if sect_match:
        # OOXML CT_PPr order: rPr must precede sectPr.
        return ppr[: sect_match.start()] + f"<w:rPr>{mark_xml}</w:rPr>" + ppr[sect_match.start():]
    close_match = re.search(r"</w:pPr>", ppr)
    if close_match:
        return ppr[: close_match.start()] + f"<w:rPr>{mark_xml}</w:rPr>" + ppr[close_match.start():]
    if not ppr:
        return f"<w:pPr><w:rPr>{mark_xml}</w:rPr></w:pPr>"
    return ppr + f"<w:rPr>{mark_xml}</w:rPr>"


def parse_document_xml(xml: bytes, *, styles: StyleRegistry | None = None) -> ParsedDocx:
    slices = locate_document_xml(xml)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValidationError(f"invalid document XML: {exc}") from exc
    body = next((child for child in root.iter() if local_name(child.tag) == "body"), None)
    if body is None:
        raise ValidationError("document XML has no body element")
    registry = styles or StyleRegistry()
    tokens = _TokenTable()
    paragraphs: list[Paragraph] = []
    slice_index = 0
    for child in list(body):
        if local_name(child.tag) != "p":
            continue
        if slice_index >= len(slices.paragraphs):
            raise ValidationError("XML paragraph locator disagrees with parsed body")
        paragraph = _parse_paragraph(child, slices.paragraphs[slice_index].raw, slice_index, registry, tokens)
        paragraphs.append(paragraph)
        slice_index += 1
    if slice_index != len(slices.paragraphs):
        raise ValidationError("XML paragraph locator missed a direct body paragraph")
    normal_style = registry.ensure(_empty_rpr(), label="Normal")
    previous_style = normal_style
    for paragraph in paragraphs:
        paragraph.base_style = choose_base_style(paragraph.nodes, previous_style)
        if paragraph.nodes and paragraph.base_style:
            previous_style = paragraph.base_style
    return ParsedDocx(TypedDocument({"schema": "1"}, paragraphs), registry, tokens.records, slices)


def _format_token_ids(tokens: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: value for key, value in sorted(tokens.items())}


def _paragraph_insertion_style(paragraph: Paragraph) -> str:
    """Recorded typing context for an empty paragraph (contract: paragraph-mark
    ``w:rPr``, then the first text run's style, then the base style)."""
    if paragraph.ppr:
        try:
            root = ET.fromstring(paragraph.ppr)
        except ET.ParseError:
            root = None
        if root is not None:
            for child in root:
                if local_name(child.tag) == "rPr":
                    return style_id_for_rpr(etree_xml(child))
    for node in paragraph.nodes:
        if isinstance(node, TextNode):
            return node.style_id
        if isinstance(node, (RangeNode, RevisionNode)):
            for child in node.children:
                if isinstance(child, TextNode):
                    return child.style_id
    return paragraph.base_style


def _relative_source_path(source_path: Path, output_dir: Path) -> str:
    try:
        return os.path.relpath(source_path, output_dir)
    except ValueError:  # Windows: source and workdir on different drives
        return str(source_path)


def extract_workdir(source: str | Path, outdir: str | Path) -> Path:
    source_path = Path(source).resolve()
    output_dir = Path(outdir).resolve()
    if not source_path.exists():
        raise ValidationError(f"file not found: {source_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source_path) as archive:
            document_xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValidationError(f"not a valid DOCX: {source_path}") from exc
    parsed = parse_document_xml(document_xml)
    template_path = output_dir / "_template.docx"
    shutil.copy2(source_path, template_path)
    styles_path = output_dir / "styles.json"
    styles_path.write_bytes(json_bytes(parsed.styles.to_json()))
    format_data: dict[str, Any] = {
        "schema": "typed-format-1",
        "model_version": 1,
        "canonicalizer_version": 1,
        "source": source_path.name,
        "source_path": _relative_source_path(source_path, output_dir),
        "source_sha256": sha256_file(source_path),
        "template": template_path.name,
        "template_sha256": sha256_file(template_path),
        "document_xml_sha256": sha256_bytes(document_xml),
        "package_manifest": zip_manifest(template_path),
        "styles_sha256": sha256_file(styles_path),
        "source_track_enabled": source_track_enabled(source_path),
        "uses_date_utc": document_uses_date_utc(document_xml),
        "paragraphs": [
            {
                "id": paragraph.paragraph_id,
                "base_style": paragraph.base_style,
                "insertion_style": _paragraph_insertion_style(paragraph),
                "skeleton": skeleton(paragraph.nodes),
                "token_ids": _token_ids(paragraph.nodes),
                "section_bearing": paragraph.section_bearing,
                "editable": paragraph.editable,
                "mark_revision": paragraph.mark_revision,
                "original_index": index,
            }
            for index, paragraph in enumerate(parsed.document.paragraphs)
        ],
        "tokens": _format_token_ids(parsed.tokens),
    }
    format_path = output_dir / "format.json"
    format_path.write_bytes(json_bytes(format_data))
    parsed.document.meta.update(
        {
            "format": format_path.name,
            "styles": styles_path.name,
            "template": template_path.name,
            "source": source_path.name,
        }
    )
    typed_path = output_dir / "typed.md"
    typed_path.write_text(serialize_typed(parsed.document), encoding="utf-8", newline="\n")
    from .edit import generate_clean_edit  # lazy: edit.py imports this module

    generate_clean_edit(output_dir, parsed.document)
    return output_dir


def _load_workdir(path: str | Path) -> tuple[Path, dict[str, Any], StyleRegistry, TypedDocument, Path]:
    workdir = Path(path).resolve()
    if not workdir.is_dir():
        raise ValidationError(f"workdir not found: {workdir}")
    required = {"typed.md", "format.json", "styles.json", "_template.docx"}
    missing = sorted(name for name in required if not (workdir / name).exists())
    if missing:
        raise ValidationError(f"workdir missing: {', '.join(missing)}")
    try:
        format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
        styles_data = json.loads((workdir / "styles.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid workdir JSON: {exc}") from exc
    if format_data.get("schema") != "typed-format-1" or format_data.get("model_version") != 1 or format_data.get("canonicalizer_version") != 1:
        raise ValidationError("incompatible typed workdir schema")
    styles = StyleRegistry.from_json(styles_data)
    typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
    template = workdir / str(format_data.get("template", "_template.docx"))
    if template.name != "_template.docx":
        raise ValidationError("template must be the workdir _template.docx")
    return workdir, format_data, styles, typed, template


def _validate_token_nodes(nodes: Iterable[Any], records: dict[str, Any]) -> None:
    for node in nodes:
        if isinstance(node, RevisionNode):
            record = records.get(node.token_id)
            if not record or record.get("kind") != "revision" or record.get("attrs", {}) != node.attrs:
                raise ValidationError(f"revision token changed or missing: {node.token_id}")
            _validate_token_nodes(node.children, records)
        elif isinstance(node, RangeNode):
            record = records.get(node.token_id)
            if not record or record.get("kind") != node.kind or record.get("attrs", {}) != node.attrs:
                raise ValidationError(f"range token changed or missing: {node.token_id}")
            _validate_token_nodes(node.children, records)
        elif isinstance(node, (AnchorNode, InlineNode, OpaqueNode)):
            record = records.get(node.token_id)
            if not record or record.get("kind") != node.kind:
                raise ValidationError(f"structural token changed or missing: {node.token_id}")
            if record.get("attrs", {}) != node.attrs:
                raise ValidationError(f"structural token attributes changed: {node.token_id}")
            if isinstance(node, InlineNode) and record.get("style_id", "") != node.style_id:
                raise ValidationError(f"inline token style changed: {node.token_id}")
        elif isinstance(node, TextNode):
            pass
        else:
            raise ValidationError("unknown typed AST node")


def _text_segments(nodes: Iterable[Any]) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    for node in nodes:
        if isinstance(node, TextNode):
            if node.text:
                segments.append((node.text, node.style_id))
        elif isinstance(node, RangeNode):
            segments.extend(_text_segments(node.children))
        elif isinstance(node, RevisionNode):
            if node.kind in ("insert", "move_to"):
                segments.extend(_text_segments(node.children))
    return segments


def _validate_segment_rewrite(
    old_segments: list[tuple[str, str]],
    new_segments: list[tuple[str, str]],
    paragraph_id: str,
) -> None:
    if len(old_segments) <= 1:
        return
    old_text = "".join(text for text, _ in old_segments)
    new_text = "".join(text for text, _ in new_segments)
    old_offsets: list[tuple[int, int]] = []
    new_offsets: list[tuple[int, int]] = []
    offset = 0
    for text, _ in old_segments:
        old_offsets.append((offset, offset + len(text)))
        offset += len(text)
    offset = 0
    for text, _ in new_segments:
        new_offsets.append((offset, offset + len(text)))
        offset += len(text)

    def touched(offsets: list[tuple[int, int]], start: int, end: int) -> int:
        return sum(1 for left, right in offsets if start < right and left < end)

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old_text, new_text, autojunk=False).get_opcodes():
        if tag != "equal" and (touched(old_offsets, i1, i2) > 1 or touched(new_offsets, j1, j2) > 1):
            raise ValidationError(f"cross-boundary text rewrite requires explicit style ownership: {paragraph_id}")
    for index, (new_text_node, _) in enumerate(new_segments):
        if index < len(old_segments) and new_text_node == old_segments[index][0]:
            continue
        if any(index != old_index and new_text_node == old_text_node for old_index, (old_text_node, _) in enumerate(old_segments)):
            raise ValidationError(f"cross-boundary text rewrite requires explicit style ownership: {paragraph_id}")


def _validate_cross_boundary_edit(baseline: Paragraph, current: Paragraph) -> None:
    _validate_segment_rewrite(
        _text_segments(baseline.nodes),
        _text_segments(current.nodes),
        current.paragraph_id,
    )


def _validate_styles(nodes: Iterable[Any], styles: StyleRegistry) -> None:
    for node in nodes:
        if isinstance(node, TextNode):
            styles.require(node.style_id)
        elif isinstance(node, InlineNode):
            if node.style_id:
                styles.require(node.style_id)
        elif isinstance(node, (RangeNode, RevisionNode)):
            _validate_styles(node.children, styles)


def _canonical_ppr(value: str) -> str:
    if not value:
        return ""
    # Paragraph-mark revisions are compared via content_signature; strip the
    # self-closing ins/del so pPr comparison stays structural.
    value = re.sub(r"<w:(ins|del)(?:\s+[^>]*)?/>", "", value)
    opening_end = value.find(">")
    if opening_end >= 0:
        opening = value[:opening_end]
        declarations: list[str] = []
        for prefix, namespace in (("w", NS_W), ("r", NS_R)):
            if f"{prefix}:" in value and f"xmlns:{prefix}=" not in opening:
                declarations.append(f'xmlns:{prefix}="{namespace}"')
        if declarations:
            value = value.replace("<w:pPr", "<w:pPr " + " ".join(declarations), 1)
    return canonical_xml(value)


def _paragraph_attrs(p_open: str) -> dict[str, str]:
    if not p_open:
        return {}
    try:
        element = ET.fromstring(p_open[:-1] + "/>" if p_open.endswith(">") else p_open)
    except ET.ParseError:
        return {}
    return {qname(key): value for key, value in element.attrib.items()}


def _new_paragraph_opening(p_open: str) -> str:
    def remove_ephemeral(match: re.Match[str]) -> str:
        name = match.group("name").rsplit(":", 1)[-1]
        return "" if name in _EPHEMERAL_P_ATTRS else match.group(0)

    return _P_ATTR_RE.sub(remove_ephemeral, p_open)


def _protected_document_bytes(xml: bytes) -> bytes:
    slices = locate_document_xml(xml)
    cursor = 0
    pieces: list[bytes] = []
    for paragraph in slices.paragraphs:
        pieces.append(xml[cursor:paragraph.start])
        cursor = paragraph.end
    pieces.append(xml[cursor:])
    return b"".join(pieces)


def package_guard(template: Path, output: Path) -> None:
    with zipfile.ZipFile(template) as source_zip, zipfile.ZipFile(output) as output_zip:
        source_names = sorted(source_zip.namelist())
        output_names = sorted(output_zip.namelist())
        if source_names != output_names:
            raise ValidationError("DOCX package part list changed")
        for name in source_names:
            if name == "word/document.xml":
                continue
            if source_zip.read(name) != output_zip.read(name):
                raise ValidationError(f"protected DOCX part changed: {name}")
        source_xml = source_zip.read("word/document.xml")
        output_xml = output_zip.read("word/document.xml")
        if _protected_document_bytes(source_xml) != _protected_document_bytes(output_xml):
            raise ValidationError("protected document XML region changed")
        try:
            ET.fromstring(output_xml)
        except ET.ParseError as exc:
            raise ValidationError(f"built document XML is invalid: {exc}") from exc


def _render_node(
    node: Any,
    base_style: str,
    styles: StyleRegistry,
    tokens: dict[str, dict[str, Any]],
    *,
    in_delete: bool = False,
) -> str:
    if isinstance(node, TextNode):
        style = styles.require(node.style_id)
        preserve = node.text[:1].isspace() or node.text[-1:].isspace()
        space = ' xml:space="preserve"' if node.text and preserve else ""
        text_tag = "delText" if in_delete else "t"
        return f"<w:r>{style.rpr}<w:{text_tag}{space}>{xml_escape(node.text)}</w:{text_tag}></w:r>"
    if isinstance(node, InlineNode):
        record = tokens.get(node.token_id)
        if not record:
            raise ValidationError(f"missing inline token: {node.token_id}")
        if node.kind == "empty-run":
            return str(record.get("raw", ""))
        style_id = node.style_id or str(record.get("style_id", ""))
        style = styles.require(style_id) if style_id else None
        raw = str(record.get("raw", ""))
        if not raw:
            raise ValidationError(f"inline token has no XML: {node.token_id}")
        return f"<w:r>{style.rpr if style else ''}{raw}</w:r>"
    if isinstance(node, AnchorNode):
        record = tokens.get(node.token_id)
        if not record or not record.get("raw"):
            raise ValidationError(f"missing anchor XML: {node.token_id}")
        return str(record["raw"])
    if isinstance(node, OpaqueNode):
        raise ValidationError(f"opaque node cannot be synthesized: {node.token_id}")
    record = tokens.get(node.token_id)
    if not record or not record.get("open") or not record.get("close"):
        raise ValidationError(f"missing range XML: {node.token_id}")
    if isinstance(node, RevisionNode):
        inner = "".join(
            _render_node(
                child,
                base_style,
                styles,
                tokens,
                in_delete=in_delete or node.kind in ("delete", "move_from"),
            )
            for child in node.children
        )
    else:
        inner = "".join(_render_node(child, base_style, styles, tokens, in_delete=in_delete) for child in node.children)
    return f"{record['open']}{inner}{record['close']}"


def _strip_paragraph_marks(ppr: str) -> str:
    """Remove self-closing paragraph-mark revisions from pPr bytes."""
    return re.sub(r"<w:(ins|del)(?:\s+[^>]*)?/>", "", ppr)


def _render_paragraph(paragraph: Paragraph, inherited: Paragraph, styles: StyleRegistry, tokens: dict[str, dict[str, Any]]) -> bytes:
    if paragraph.inherit:
        p_open = paragraph.p_open or inherited.p_open
        ppr = paragraph.ppr or inherited.ppr
    else:
        p_open = paragraph.p_open
        ppr = paragraph.ppr
    if not p_open:
        raise ValidationError(f"paragraph {paragraph.paragraph_id} has no template opening")
    # Template pPr bytes may already carry paragraph marks (extract keeps them
    # verbatim); render them from the AST state only, so a resolved mark
    # (mark_revision=None) disappears instead of surviving in the pPr bytes.
    if paragraph.mark_revision is not None or _MARK_TAG_RE.search(ppr) is not None:
        ppr = _strip_paragraph_marks(ppr)
    if paragraph.mark_revision:
        ppr = _inject_mark_revision(ppr, paragraph.mark_revision, tokens)
    body = "".join(_render_node(node, paragraph.base_style, styles, tokens) for node in paragraph.nodes)
    return (p_open + ppr + body + "</w:p>").encode("utf-8")


def _paragraph_placements(paragraphs: list[Paragraph], template_count: int) -> tuple[list[int | None], list[int | None]]:
    existing_order = [paragraph.original_index for paragraph in paragraphs if paragraph.original_index >= 0]
    if existing_order != sorted(existing_order):
        raise ValidationError("existing paragraph order cannot change")
    slots: list[int | None] = []
    insert_before: list[int | None] = []
    for index, paragraph in enumerate(paragraphs):
        if paragraph.original_index >= 0:
            slots.append(paragraph.original_index)
            insert_before.append(None)
            continue
        target = template_count
        for following in paragraphs[index + 1:]:
            if following.original_index >= 0:
                target = following.original_index
                break
        slots.append(None)
        insert_before.append(target)
    return slots, insert_before


def patch_document_xml(
    xml: bytes,
    slices: DocumentSlices,
    replacements: list[bytes],
    slots: list[int | None] | None = None,
    insert_before: list[int | None] | None = None,
) -> bytes:
    if slots is None:
        slots = list(range(len(replacements)))
    if len(slots) != len(replacements):
        raise ValidationError("paragraph replacement slots do not match replacements")
    if insert_before is not None and len(insert_before) != len(replacements):
        raise ValidationError("paragraph insertion positions do not match replacements")
    before: dict[int, list[bytes]] = {}
    replacement_by_slot: dict[int, bytes] = {}
    for index, (slot, replacement) in enumerate(zip(slots, replacements)):
        if slot is None:
            target = insert_before[index] if insert_before is not None else len(slices.paragraphs)
            if target is None:
                target = len(slices.paragraphs)
            if target < 0 or target > len(slices.paragraphs):
                raise ValidationError(f"invalid paragraph insertion position: {target}")
            before.setdefault(target, []).append(replacement)
        elif slot in replacement_by_slot:
            raise ValidationError(f"duplicate replacement for paragraph slot {slot}")
        else:
            replacement_by_slot[slot] = replacement
    output: list[bytes] = []
    cursor = 0
    for index, paragraph in enumerate(slices.paragraphs):
        output.append(xml[cursor:paragraph.start])
        output.extend(before.get(index, ()))
        if index in replacement_by_slot:
            output.append(replacement_by_slot[index])
        cursor = paragraph.end
    output.extend(before.get(len(slices.paragraphs), ()))
    output.append(xml[cursor:])
    return b"".join(output)


def scan_package_revisions(path: Path) -> list[dict[str, Any]]:
    """Read-only inventory of tracked revisions across all WordprocessingML
    parts (document, headers, footers, footnotes, endnotes, comments)."""
    from .typed_core import NS_W

    revisions: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not name.endswith(".xml"):
                continue
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                continue
            for element in root.iter():
                local = local_name(element.tag)
                if local not in {"ins", "del", "moveFrom", "moveTo"}:
                    continue
                attrs = {
                    qname(key): value
                    for key, value in element.attrib.items()
                    if key != f"{{{NS_W}}}id"
                }
                text_parts: list[str] = []
                for child in element.iter():
                    if local_name(child.tag) in {"t", "delText"}:
                        text_parts.append(child.text or "")
                revisions.append(
                    {
                        "part": name,
                        "kind": {
                            "ins": "insert",
                            "del": "delete",
                            "moveFrom": "move_from",
                            "moveTo": "move_to",
                        }[local],
                        "w_id": element.attrib.get(f"{{{NS_W}}}id", ""),
                        "author": attrs.get("w:author", ""),
                        "date": attrs.get("w:date", ""),
                        "text": "".join(text_parts),
                    }
                )
    return revisions


def source_track_enabled(source_path: Path) -> bool:
    """Whether the source package has track changes enabled (settings.xml)."""
    from .typed_core import NS_W

    with zipfile.ZipFile(source_path) as archive:
        try:
            settings_xml = archive.read("word/settings.xml")
        except KeyError:
            return False
    try:
        root = ET.fromstring(settings_xml)
    except ET.ParseError:
        return False
    return any(local_name(child.tag) == "trackChanges" for child in root.iter())


def document_uses_date_utc(document_xml: bytes) -> bool:
    """Whether the source document already uses w16du:dateUtc on revisions."""
    return b"dateUtc" in document_xml


def used_revision_ids(path: Path) -> set[int]:
    """Package-wide set of w:id values used by tracked revisions."""
    from .typed_core import NS_W

    ids: set[int] = set()
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not name.endswith(".xml"):
                continue
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                continue
            for element in root.iter():
                local = local_name(element.tag)
                if local not in {"ins", "del", "moveFrom", "moveTo"}:
                    continue
                raw = element.attrib.get(f"{{{NS_W}}}id", "")
                if raw.isdigit():
                    ids.add(int(raw))
    return ids


def next_revision_id(path: Path, used: set[int] | None = None) -> int:
    """Lowest available non-negative w:id over the package (and ``used``)."""
    taken = used_revision_ids(path)
    if used:
        taken = taken | set(used)
    candidate = 0
    while candidate in taken:
        candidate += 1
    return candidate


def _write_patched_docx(template: Path, output: Path, document_xml: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(template) as source_zip, zipfile.ZipFile(output, "w") as output_zip:
        for info in source_zip.infolist():
            data = document_xml if info.filename == "word/document.xml" else source_zip.read(info.filename)
            output_zip.writestr(info, data)


def validate_workdir(path: str | Path) -> ValidatedWorkdir:
    workdir, format_data, styles, typed, template = _load_workdir(path)
    if sha256_file(workdir / "styles.json") != format_data.get("styles_sha256"):
        raise ValidationError("styles.json changed after extract")
    if sha256_file(template) != format_data.get("template_sha256"):
        raise ValidationError("template fingerprint changed after extract")
    source_value = str(format_data.get("source_path", ""))
    if source_value:
        source_ref = Path(source_value)
        source_path = source_ref if source_ref.is_absolute() else workdir / source_ref
        if source_path.exists() and sha256_file(source_path) != format_data.get("source_sha256"):
            raise ValidationError("source fingerprint changed after extract")
    current_manifest = zip_manifest(template)
    if current_manifest != format_data.get("package_manifest"):
        raise ValidationError("template package manifest changed after extract")
    with zipfile.ZipFile(template) as archive:
        template_xml = archive.read("word/document.xml")
    if sha256_bytes(template_xml) != format_data.get("document_xml_sha256"):
        raise ValidationError("template document.xml fingerprint changed after extract")
    parsed = parse_document_xml(template_xml)
    if set(parsed.styles.styles) != set(styles.styles):
        raise ValidationError("style registry does not match template styles")
    for style_id, style in parsed.styles.styles.items():
        if styles.styles[style_id].canonical != style.canonical:
            raise ValidationError(f"style registry differs from template: {style_id}")
    records = format_data.get("paragraphs", [])
    if len(records) != len(parsed.document.paragraphs):
        raise ValidationError("format paragraph baseline does not match template")
    baseline_by_id: dict[str, Paragraph] = {}
    for paragraph, record in zip(parsed.document.paragraphs, records):
        paragraph.paragraph_id = record["id"]
        if paragraph.paragraph_id in baseline_by_id:
            raise ValidationError(f"duplicate baseline paragraph ID: {paragraph.paragraph_id}")
        baseline_by_id[paragraph.paragraph_id] = paragraph
        if record.get("base_style") != paragraph.base_style:
            raise ValidationError(f"base style baseline differs for {paragraph.paragraph_id}")
        if record.get("insertion_style") != _paragraph_insertion_style(paragraph):
            raise ValidationError(f"insertion style baseline differs for {paragraph.paragraph_id}")
        if record.get("skeleton") != skeleton(paragraph.nodes):
            raise ValidationError(f"structure skeleton differs for {paragraph.paragraph_id}")
        if record.get("token_ids") != _token_ids(paragraph.nodes):
            raise ValidationError(f"structure token IDs differ for {paragraph.paragraph_id}")
        if bool(record.get("section_bearing")) != paragraph.section_bearing:
            raise ValidationError(f"section-bearing baseline differs for {paragraph.paragraph_id}")
        if (record.get("mark_revision") or None) != paragraph.mark_revision:
            raise ValidationError(f"paragraph-mark baseline differs for {paragraph.paragraph_id}")
    _validate_anchor_pairs(parsed.document.paragraphs, "template")
    expected_header = {
        "schema": "1",
        "format": "format.json",
        "styles": "styles.json",
        "template": "_template.docx",
    }
    for key, value in expected_header.items():
        if typed.meta.get(key) != value:
            raise ValidationError(f"typed header {key} must be {value}")
    baseline_ids = set(baseline_by_id)
    live_id_list = [paragraph.paragraph_id for paragraph in typed.paragraphs]
    if len(live_id_list) != len(set(live_id_list)):
        raise ValidationError("duplicate live paragraph ID")
    delete_id_list = list(typed.deletions)
    if len(delete_id_list) != len(set(delete_id_list)):
        raise ValidationError("duplicate paragraph deletion tombstone")
    live_ids = set(live_id_list)
    delete_ids = set(delete_id_list)
    if live_ids & delete_ids:
        raise ValidationError("paragraph cannot be both live and deleted")
    unknown_deletes = delete_ids - baseline_ids
    if unknown_deletes:
        unknown_text = ", ".join(sorted(unknown_deletes))
        raise ValidationError(f"unknown paragraph IDs in deletion tombstones: {unknown_text}")
    missing = baseline_ids - live_ids - delete_ids
    if missing:
        raise ValidationError("missing explicit deletion tombstone for: " + ", ".join(sorted(missing)))
    records_by_id = {record["id"]: record for record in records}
    live_paragraphs: list[Paragraph] = []
    warnings: list[str] = []
    for paragraph in typed.paragraphs:
        if paragraph.paragraph_id in baseline_by_id:
            baseline = baseline_by_id[paragraph.paragraph_id]
            record = records_by_id[paragraph.paragraph_id]
            if paragraph.inherit:
                raise ValidationError(f"existing paragraph cannot use inherit: {paragraph.paragraph_id}")
            if paragraph.base_style != baseline.base_style:
                raise ValidationError(f"base style changed: {paragraph.paragraph_id}")
            synced_segments = record.get("sync_segments")
            if synced_segments is None:
                if skeleton(paragraph.nodes) != record.get("skeleton"):
                    raise ValidationError(f"structure skeleton changed: {paragraph.paragraph_id}")
                if _token_ids(paragraph.nodes) != record.get("token_ids"):
                    raise ValidationError(f"structure token IDs changed: {paragraph.paragraph_id}")
                _validate_token_nodes(paragraph.nodes, format_data.get("tokens", {}))
                _validate_styles(paragraph.nodes, styles)
                _validate_cross_boundary_edit(baseline, paragraph)
            else:
                governed_segments = [(segment[1], segment[0]) for segment in synced_segments]
                if _text_segments(paragraph.nodes) != governed_segments:
                    if skeleton(paragraph.nodes) != record.get("sync_skeleton"):
                        raise ValidationError(f"structure skeleton changed: {paragraph.paragraph_id}")
                    _validate_token_nodes(paragraph.nodes, format_data.get("tokens", {}))
                    _validate_styles(paragraph.nodes, styles)
                    _validate_segment_rewrite(
                        governed_segments,
                        _text_segments(paragraph.nodes),
                        paragraph.paragraph_id,
                    )
                else:
                    _validate_token_nodes(paragraph.nodes, format_data.get("tokens", {}))
                    _validate_styles(paragraph.nodes, styles)
            if content_signature(paragraph) != content_signature(baseline) and contains_opaque(baseline.nodes):
                raise ValidationError(f"unsupported opaque paragraph was edited: {paragraph.paragraph_id}")
            paragraph.p_open = baseline.p_open
            paragraph.ppr = baseline.ppr
            paragraph.section_bearing = baseline.section_bearing
            paragraph.original_index = baseline.original_index
        else:
            if not paragraph.inherit or paragraph.inherit not in baseline_by_id:
                raise ValidationError(f"new paragraph requires existing inherit ID: {paragraph.paragraph_id}")
            inherited = baseline_by_id[paragraph.inherit]
            if inherited.section_bearing or _contains_structural(inherited.nodes):
                raise ValidationError(
                    f"new paragraph cannot inherit protected structure: {paragraph.paragraph_id}"
                )
            _assign_default_style(paragraph.nodes, inherited.base_style)
            if _contains_structural(paragraph.nodes):
                raise ValidationError(f"new paragraph cannot add structural tokens: {paragraph.paragraph_id}")
            _validate_styles(paragraph.nodes, styles)
            paragraph.base_style = inherited.base_style
            paragraph.p_open = _new_paragraph_opening(inherited.p_open)
            paragraph.ppr = inherited.ppr
            paragraph.section_bearing = False
            paragraph.original_index = -1
        live_paragraphs.append(paragraph)
    existing_order = [paragraph.original_index for paragraph in live_paragraphs if paragraph.original_index >= 0]
    if existing_order != sorted(existing_order):
        raise ValidationError("existing paragraph order cannot change")
    for deleted_id in typed.deletions:
        baseline = baseline_by_id[deleted_id]
        if baseline.section_bearing or _contains_structural(baseline.nodes):
            raise ValidationError(f"paragraph with protected structure cannot be deleted: {deleted_id}")
    _validate_anchor_pairs(live_paragraphs, "typed source")
    used_styles: set[str] = set()
    for paragraph in live_paragraphs:
        for node in paragraph.nodes:
            if isinstance(node, TextNode):
                used_styles.add(node.style_id)
    unused = sorted(set(styles.styles) - used_styles)
    if unused:
        warnings.append("unused styles: " + ", ".join(unused))
    return ValidatedWorkdir(
        workdir,
        format_data,
        styles,
        typed,
        parsed.document,
        format_data.get("tokens", {}),
        template,
        template_xml,
        parsed.slices,
        live_paragraphs,
        baseline_by_id,
        warnings,
    )


def build_workdir(path: str | Path, output: str | Path | None = None) -> Path:
    validated = validate_workdir(path)
    from .edit import require_clean_edit  # lazy: edit.py imports this module

    require_clean_edit(path)
    output_path = Path(output).resolve() if output else validated.path.parent / f"{validated.path.name}.docx"
    reserved_paths = {
        (validated.path / name).resolve()
        for name in {"_template.docx", "typed.md", "format.json", "styles.json"}
    }
    source_value = str(validated.format_data.get("source_path", ""))
    if source_value:
        source_ref = Path(source_value)
        source_path = source_ref if source_ref.is_absolute() else validated.path / source_ref
        reserved_paths.add(source_path.resolve())
    if output_path in reserved_paths:
        raise ValidationError(f"output path is reserved: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    replacements: list[bytes] = []
    for paragraph in validated.live_paragraphs:
        if not paragraph.inherit:
            baseline = validated.baseline_by_id[paragraph.paragraph_id]
            if content_signature(paragraph) == content_signature(baseline):
                replacements.append(baseline.raw_xml.encode("utf-8"))
                continue
            replacements.append(_render_paragraph(paragraph, baseline, validated.styles, validated.format_data.get("tokens", {})))
        else:
            inherited = validated.baseline_by_id[paragraph.inherit]
            replacements.append(_render_paragraph(paragraph, inherited, validated.styles, validated.format_data.get("tokens", {})))
    slots, insert_before = _paragraph_placements(validated.live_paragraphs, len(validated.template_slices.paragraphs))
    patched_xml = patch_document_xml(
        validated.template_xml,
        validated.template_slices,
        replacements,
        slots,
        insert_before,
    )
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".typed-build-", suffix=".docx", dir=output_path.parent, delete=False) as temp:
            temp_name = temp.name
        temp_path = Path(temp_name)
        _write_patched_docx(validated.template_path, temp_path, patched_xml)
        package_guard(validated.template_path, temp_path)
        verify_workdir(validated.path, temp_path)
        os.replace(temp_path, output_path)
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()
    return output_path


def _compare_output_paragraph(
    expected: Paragraph,
    actual: Paragraph,
    tokens: dict[str, dict[str, Any]],
) -> None:
    expected_ppr = expected.ppr
    if expected.mark_revision:
        expected_ppr = _inject_mark_revision(expected_ppr, expected.mark_revision, tokens)
    if _canonical_ppr(expected_ppr) != _canonical_ppr(actual.ppr):
        raise ValidationError(f"output paragraph properties differ: {expected.paragraph_id}")
    if _paragraph_attrs(expected.p_open) != _paragraph_attrs(actual.p_open):
        raise ValidationError(f"output paragraph attributes differ: {expected.paragraph_id}")
    if content_signature(expected) != content_signature(actual):
        raise ValidationError(f"output text or structure differs: {expected.paragraph_id}")
    # three-layer revision verification: final view, original view, structure.
    if visible_text(expected.nodes) != visible_text(actual.nodes):
        raise ValidationError(f"output final-view text differs: {expected.paragraph_id}")
    if visible_text_original(expected.nodes) != visible_text_original(actual.nodes):
        raise ValidationError(f"output original-view text differs: {expected.paragraph_id}")


def verify_workdir(path: str | Path, output: str | Path) -> None:
    validated = validate_workdir(path)
    from .edit import require_clean_edit  # lazy: edit.py imports this module

    require_clean_edit(path)
    output_path = Path(output).resolve()
    if not output_path.exists():
        raise ValidationError(f"output DOCX not found: {output_path}")
    package_guard(validated.template_path, output_path)
    with zipfile.ZipFile(output_path) as archive:
        output_xml = archive.read("word/document.xml")
    output_parsed = parse_document_xml(output_xml)
    if len(output_parsed.document.paragraphs) != len(validated.live_paragraphs):
        raise ValidationError(
            f"output direct paragraph count differs: expected {len(validated.live_paragraphs)}, got {len(output_parsed.document.paragraphs)}"
        )
    expected: list[Paragraph] = []
    for paragraph in validated.live_paragraphs:
        expected.append(paragraph)
    for index, (wanted, actual) in enumerate(zip(expected, output_parsed.document.paragraphs)):
        actual.paragraph_id = wanted.paragraph_id
        if not wanted.inherit:
            baseline = validated.baseline_by_id[wanted.paragraph_id]
            if content_signature(wanted) == content_signature(baseline):
                expected_raw = baseline.raw_xml.encode("utf-8")
                if output_parsed.slices.paragraphs[index].raw != expected_raw:
                    raise ValidationError(f"untouched paragraph bytes differ: {wanted.paragraph_id}")
        _compare_output_paragraph(wanted, actual, validated.format_data.get("tokens", {}))

def extract(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docx2typed extract")
    parser.add_argument("input", help="source .docx")
    parser.add_argument("-o", "--outdir", default=".", help="typed workdir")
    args = parser.parse_args(argv)
    try:
        workdir = extract_workdir(args.input, args.outdir)
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"workdir:  {workdir}")
    print(f"typed:    {workdir / 'typed.md'}")
    print(f"format:   {workdir / 'format.json'}")
    print(f"styles:   {workdir / 'styles.json'}")
    print(f"template: {workdir / '_template.docx'}")
    return 0


def validate(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docx2typed validate")
    parser.add_argument("workdir", help="typed workdir")
    args = parser.parse_args(argv)
    try:
        checked = validate_workdir(args.workdir)
        from .edit import require_clean_edit  # lazy: edit.py imports this module

        require_clean_edit(args.workdir)
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"validated: {checked.path}")
    for warning in checked.warnings:
        print(f"warning: {warning}")
    return 0


def build(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docx2typed build")
    parser.add_argument("workdir", help="typed workdir")
    parser.add_argument("-o", "--output", help="output .docx")
    args = parser.parse_args(argv)
    try:
        output = build_workdir(args.workdir, args.output)
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"built: {output}")
    return 0


def verify(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docx2typed verify")
    parser.add_argument("workdir", help="typed workdir")
    parser.add_argument("output", help="built .docx")
    args = parser.parse_args(argv)
    try:
        verify_workdir(args.workdir, args.output)
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print("verified: typed source, XML structure, and package integrity")
    return 0
