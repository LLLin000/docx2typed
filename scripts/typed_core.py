"""The restricted typed-mode model and parser.

This module deliberately does not use a Markdown or HTML parser.  Typed mode
is a small, project-owned language whose only editable meaning is text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from xml.sax.saxutils import quoteattr

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_XML = "http://www.w3.org/XML/1998/namespace"
NS_W16DU = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"

ET.register_namespace("w", NS_W)
ET.register_namespace("r", NS_R)
ET.register_namespace("w16du", NS_W16DU)


class TypedError(ValueError):
    """A user-facing typed grammar or model error."""


def w(local_name: str) -> str:
    return f"{{{NS_W}}}{local_name}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


def namespace_uri(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def qname(tag: str) -> str:
    uri = namespace_uri(tag)
    local = local_name(tag)
    if uri == NS_W:
        return f"w:{local}"
    if uri == NS_R:
        return f"r:{local}"
    if uri == NS_XML:
        return f"xml:{local}"
    if uri == NS_W16DU:
        return f"w16du:{local}"
    return f"{{{uri}}}{local}" if uri else local


def attr_name(tag: str) -> str:
    return qname(tag)


def attr_value(value: str) -> str:
    return '"' + xml_escape(value).replace('"', "&quot;") + '"'


def xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ALLOWED_ENTITIES = ("&amp;", "&lt;", "&gt;")


def xml_unescape(text: str) -> str:
    cursor = 0
    while True:
        marker = text.find("&", cursor)
        if marker < 0:
            break
        if not any(text.startswith(entity, marker) for entity in _ALLOWED_ENTITIES):
            raise TypedError(f"unknown or unescaped entity at offset {marker}")
        cursor = marker + 1
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def etree_xml(element: ET.Element) -> str:
    return ET.tostring(element, encoding="unicode", short_empty_elements=True)


def element_start_xml(element: ET.Element) -> str:
    attrs = " ".join(f"{attr_name(key)}={attr_value(value)}" for key, value in element.attrib.items())
    return f"<{qname(element.tag)}{(' ' + attrs) if attrs else ''}>"


def element_end_xml(element: ET.Element) -> str:
    return f"</{qname(element.tag)}>"


_BOOLEAN_RPR = {
    "b", "i", "strike", "outline", "shadow", "smallCaps", "caps", "vanish", "webHidden"
}
_FALSE_VALUES = {"false", "0", "off", "none"}
_TRUE_VALUES = {"true", "1", "on"}


def _canonical_element(element: ET.Element, *, rpr: bool = False) -> tuple[Any, ...] | None:
    element_name = local_name(element.tag)
    normalized_attrs: list[tuple[str, str]] = []
    for key, value in element.attrib.items():
        key_local = local_name(key)
        if key_local.startswith("rsid"):
            continue
        if rpr and element_name in _BOOLEAN_RPR and key_local == "val":
            lowered = value.lower()
            if lowered in _FALSE_VALUES:
                return None
            if lowered in _TRUE_VALUES:
                continue
        normalized_attrs.append((qname(key), value))
    normalized_attrs.sort()
    children: list[tuple[Any, ...]] = []
    for child in list(element):
        canonical = _canonical_element(child, rpr=rpr)
        if canonical is not None:
            children.append(canonical)
    if rpr:
        children.sort(key=repr)
    text = element.text or ""
    return (qname(element.tag), tuple(normalized_attrs), text, tuple(children))


def canonical_xml(fragment: str, *, rpr: bool = False) -> str:
    try:
        element = ET.fromstring(fragment)
    except ET.ParseError as exc:
        raise TypedError(f"invalid XML fragment: {exc}") from exc
    canonical = _canonical_element(element, rpr=rpr)
    return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))


def canonical_rpr(rpr_xml: str) -> str:
    fragment = rpr_xml or f'<w:rPr xmlns:w="{NS_W}"/>'
    try:
        root = ET.fromstring(fragment)
    except ET.ParseError:
        return canonical_xml(fragment, rpr=True)
    for child in list(root):
        if local_name(child.tag) == "rPrChange":
            root.remove(child)  # format history is not a format property
    return canonical_xml(etree_xml(root), rpr=True)


def style_id_for_rpr(rpr_xml: str) -> str:
    digest = hashlib.sha256(canonical_rpr(rpr_xml).encode("utf-8")).hexdigest()
    return "s_" + digest[:16]


def rpr_features(rpr_xml: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(rpr_xml or f'<w:rPr xmlns:w="{NS_W}"/>')
    except ET.ParseError:
        return {}
    features: dict[str, Any] = {}
    for child in root:
        name = local_name(child.tag)
        value = child.attrib.get(w("val"), child.attrib.get("val", "true"))
        if name in _BOOLEAN_RPR:
            if str(value).lower() not in _FALSE_VALUES:
                features[name] = True
        elif name == "rFonts":
            for key, val in child.attrib.items():
                features[f"font:{local_name(key)}"] = val
        elif name in {
            "vertAlign",
            "position",
            "color",
            "sz",
            "szCs",
            "highlight",
            "lang",
            "u",
            "kern",
            "spacing",
            "w",
            "rStyle",
            "em",
            "rtl",
            "cs",
            "textEffect",
        }:
            features[name] = value
    return features


def style_label(rpr_xml: str) -> str:
    features = rpr_features(rpr_xml)
    labels: list[str] = []
    for name in ("b", "i", "strike", "dstrike", "smallCaps", "caps", "outline", "imprint"):
        if features.get(name):
            labels.append(
                {
                    "b": "bold",
                    "i": "italic",
                    "strike": "strike",
                    "dstrike": "double-strike",
                    "smallCaps": "small-caps",
                    "caps": "caps",
                    "outline": "outline",
                    "imprint": "imprint",
                }[name]
            )
    fonts: list[str] = []
    for key in sorted(features):
        if key.startswith("font:") and key.split(":", 1)[1] in {"ascii", "eastAsia", "hAnsi", "cs"}:
            fonts.append(str(features[key]))
    if fonts:
        labels.append("/".join(dict.fromkeys(fonts)))
    for key in ("sz", "szCs", "vertAlign", "position", "color", "u", "highlight", "lang", "kern", "spacing", "w", "rStyle", "em", "rtl", "cs", "textEffect"):
        if key in features:
            labels.append(f"{key}={features[key]}")
    return ", ".join(labels) if labels else "normal"


@dataclass
class Style:
    style_id: str
    rpr: str
    canonical: str
    label: str
    features: dict[str, Any] = field(default_factory=dict)


class StyleRegistry:
    """Immutable-by-convention content-addressed character styles."""

    def __init__(self, styles: dict[str, Style] | None = None) -> None:
        self.styles: dict[str, Style] = styles or {}

    def ensure(self, rpr_xml: str, *, label: str | None = None) -> str:
        rpr_xml = rpr_xml or f'<w:rPr xmlns:w="{NS_W}"/>'
        canonical = canonical_rpr(rpr_xml)
        style_id = "s_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        existing = self.styles.get(style_id)
        if existing is not None:
            if existing.canonical != canonical:
                raise TypedError(f"style hash collision for {style_id}")
            return style_id
        self.styles[style_id] = Style(
            style_id=style_id,
            rpr=rpr_xml,
            canonical=canonical,
            label=label or style_label(rpr_xml),
            features=rpr_features(rpr_xml),
        )
        return style_id

    def require(self, style_id: str) -> Style:
        try:
            return self.styles[style_id]
        except KeyError as exc:
            raise TypedError(f"unknown style ID: {style_id}") from exc

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "typed-styles-1",
            "canonicalizer_version": 1,
            "styles": {
                key: {
                    "rPr": value.rpr,
                    "canonical": value.canonical,
                    "label": value.label,
                    "features": value.features,
                }
                for key, value in sorted(self.styles.items())
            },
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StyleRegistry":
        if data.get("schema") != "typed-styles-1" or data.get("canonicalizer_version") != 1:
            raise TypedError("incompatible style registry schema")
        styles: dict[str, Style] = {}
        for style_id, raw in data.get("styles", {}).items():
            rpr = raw.get("rPr", "")
            canonical = canonical_rpr(rpr)
            expected = "s_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
            if style_id != expected or raw.get("canonical") != canonical:
                raise TypedError(f"style registry entry {style_id} has invalid canonical identity")
            styles[style_id] = Style(
                style_id=style_id,
                rpr=rpr,
                canonical=canonical,
                label=raw.get("label") or style_label(rpr),
                features=raw.get("features") or rpr_features(rpr),
            )
        return cls(styles)


@dataclass
class TextNode:
    style_id: str
    text: str


@dataclass
class AnchorNode:
    token_id: str
    kind: str
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class InlineNode:
    token_id: str
    kind: str
    style_id: str = ""
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class OpaqueNode:
    token_id: str
    kind: str
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class RangeNode:
    token_id: str
    kind: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)


@dataclass
class RevisionNode:
    """A Word tracked-change container (w:ins/w:del/w:moveFrom/w:moveTo).

    Children are parsed normally (runs, nested revisions, hyperlinks,
    anchors); TextNode stays neutral — rendering picks ``w:t`` vs
    ``w:delText`` from the ancestor context, so rejecting a deletion is just
    unwrapping this node. ``attrs`` carries the raw OOXML attributes
    (``w:id``, ``w:author``, ``w:date``, ``w16du:dateUtc``, ...); the exact
    open/close XML is kept in the token table for byte-faithful rendering.
    """

    token_id: str
    kind: str  # "insert" | "delete" | "move_from" | "move_to"
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)


Node = TextNode | AnchorNode | InlineNode | OpaqueNode | RangeNode | RevisionNode


@dataclass
class Paragraph:
    paragraph_id: str
    base_style: str
    nodes: list[Node] = field(default_factory=list)
    p_open: str = ""
    ppr: str = ""
    raw_xml: str = ""
    section_bearing: bool = False
    editable: bool = True
    inherit: str = ""
    original_index: int = -1
    mark_revision: dict[str, Any] | None = None


@dataclass
class TypedDocument:
    meta: dict[str, str]
    paragraphs: list[Paragraph] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)


def merge_adjacent_text(nodes: list[Node]) -> list[Node]:
    merged: list[Node] = []
    for node in nodes:
        if isinstance(node, (RangeNode, RevisionNode)):
            node.children = merge_adjacent_text(node.children)
        if isinstance(node, TextNode) and merged and isinstance(merged[-1], TextNode) and merged[-1].style_id == node.style_id:
            merged[-1].text += node.text
        else:
            merged.append(node)
    return merged


def visible_text(nodes: Iterable[Node]) -> str:
    """Final-view text (Word No Markup): insertions and move-to visible,
    deletions and move-from hidden."""
    result: list[str] = []
    for node in nodes:
        if isinstance(node, TextNode):
            result.append(node.text)
        elif isinstance(node, RangeNode):
            result.append(visible_text(node.children))
        elif isinstance(node, RevisionNode):
            if node.kind in ("insert", "move_to"):
                result.append(visible_text(node.children))
        elif isinstance(node, InlineNode):
            if node.kind in {"tab"}:
                result.append("\t")
            elif node.kind in {"br", "cr"}:
                result.append("\n")
        elif isinstance(node, OpaqueNode):
            result.append(f"[opaque:{node.kind}]")
    return "".join(result)


def visible_text_original(nodes: Iterable[Node]) -> str:
    """Original-view text (Word Original): deletions and move-from visible,
    insertions and move-to hidden."""
    result: list[str] = []
    for node in nodes:
        if isinstance(node, TextNode):
            result.append(node.text)
        elif isinstance(node, RangeNode):
            result.append(visible_text_original(node.children))
        elif isinstance(node, RevisionNode):
            if node.kind in ("delete", "move_from"):
                result.append(visible_text_original(node.children))
        elif isinstance(node, InlineNode):
            if node.kind in {"tab"}:
                result.append("\t")
            elif node.kind in {"br", "cr"}:
                result.append("\n")
    return "".join(result)


def visible_char_count(nodes: Iterable[Node]) -> int:
    return sum(len(node.text) for node in nodes if isinstance(node, TextNode)) + sum(
        visible_char_count(node.children) for node in nodes if isinstance(node, RangeNode)
    )


def contains_opaque(nodes: Iterable[Node]) -> bool:
    return any(
        isinstance(node, OpaqueNode)
        or (
            isinstance(node, (RangeNode, RevisionNode))
            and contains_opaque(node.children)
        )
        for node in nodes
    )


def skeleton(nodes: Iterable[Node]) -> list[Any]:
    result: list[Any] = []
    for node in nodes:
        if isinstance(node, TextNode):
            result.append(["text", node.style_id])
        elif isinstance(node, AnchorNode):
            result.append(["anchor", node.kind, [[key, value] for key, value in sorted(node.attrs.items())]])
        elif isinstance(node, InlineNode):
            result.append(["inline", node.kind, node.style_id, [[key, value] for key, value in sorted(node.attrs.items())]])
        elif isinstance(node, OpaqueNode):
            result.append(["opaque", node.kind, [[key, value] for key, value in sorted(node.attrs.items())]])
        elif isinstance(node, RangeNode):
            result.append(["range", node.kind, [[key, value] for key, value in sorted(node.attrs.items())], skeleton(node.children)])
        elif isinstance(node, RevisionNode):
            result.append(["revision", node.kind, [[key, value] for key, value in sorted(node.attrs.items())], skeleton(node.children)])
    return result


def content_signature(paragraph: Paragraph) -> tuple[Any, ...]:
    def content(nodes: Iterable[Node]) -> list[Any]:
        values: list[Any] = []
        for node in nodes:
            if isinstance(node, TextNode):
                values.append(("text", node.style_id, node.text))
            elif isinstance(node, RangeNode):
                values.append(("range", node.kind, tuple(sorted(node.attrs.items())), tuple(content(node.children))))
            elif isinstance(node, RevisionNode):
                values.append(("revision", node.kind, tuple(sorted(node.attrs.items())), tuple(content(node.children))))
            elif isinstance(node, AnchorNode):
                values.append(("anchor", node.kind, tuple(sorted(node.attrs.items()))))
            elif isinstance(node, InlineNode):
                values.append(("inline", node.kind, node.style_id, tuple(sorted(node.attrs.items()))))
            elif isinstance(node, OpaqueNode):
                values.append(("opaque", node.kind, tuple(sorted(node.attrs.items()))))
        return values

    def _mark_signature(mark: dict[str, Any] | None) -> Any:
        if mark is None:
            return None
        return (
            mark["kind"],
            tuple(sorted(mark.get("attrs", {}).items())),
        )

    return (tuple(content(paragraph.nodes)), _mark_signature(paragraph.mark_revision))


def choose_base_style(nodes: Iterable[Node], fallback: str) -> str:
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    order = 0

    def visit(items: Iterable[Node]) -> None:
        nonlocal order
        for node in items:
            if isinstance(node, TextNode):
                counts[node.style_id] = counts.get(node.style_id, 0) + len(node.text)
                first.setdefault(node.style_id, order)
                order += 1
            elif isinstance(node, RangeNode):
                visit(node.children)
            elif isinstance(node, RevisionNode):
                if node.kind in ("insert", "move_to"):
                    visit(node.children)  # deleted text does not count in final view

    visit(nodes)
    if not counts:
        return fallback
    return max(counts, key=lambda style: (counts[style], -first[style]))


def _attrs_text(attrs: dict[str, str], *, first: tuple[str, ...] = ()) -> str:
    order = list(first) + sorted(key for key in attrs if key not in first)
    return " ".join(f"{key}={attr_value(attrs[key])}" for key in order if key in attrs)


def _node_to_markup(node: Node, base_style: str) -> str:
    first_attrs = ("id", "kind")
    if isinstance(node, TextNode):
        text = xml_escape(node.text)
        if node.style_id == base_style:
            return text
        return f'<span data-s={attr_value(node.style_id)}>{text}</span>'
    if isinstance(node, AnchorNode):
        attrs = {"id": node.token_id, "kind": node.kind, **node.attrs}
        return f"<docx-anchor {_attrs_text(attrs, first=first_attrs)}/>"
    if isinstance(node, InlineNode):
        attrs = {"id": node.token_id, "kind": node.kind, **node.attrs}
        if node.style_id:
            attrs["style"] = node.style_id
        return f"<docx-inline {_attrs_text(attrs, first=first_attrs)}/>"
    if isinstance(node, OpaqueNode):
        attrs = {"id": node.token_id, "kind": node.kind, **node.attrs}
        return f"<docx-opaque {_attrs_text(attrs, first=first_attrs)}/>"
    if isinstance(node, RevisionNode):
        attrs = {"id": node.token_id, "kind": node.kind, **node.attrs}
        inner = "".join(_node_to_markup(child, base_style) for child in node.children)
        return f"<docx-revision {_attrs_text(attrs, first=first_attrs)}>{inner}</docx-revision>"
    attrs = {"id": node.token_id, "kind": node.kind, **node.attrs}
    inner = "".join(_node_to_markup(child, base_style) for child in node.children)
    return f"<docx-range {_attrs_text(attrs, first=first_attrs)}>{inner}</docx-range>"


def serialize_typed(document: TypedDocument) -> str:
    meta = document.meta
    header_attrs = {
        "schema": meta.get("schema", "1"),
        "format": meta.get("format", "format.json"),
        "styles": meta.get("styles", "styles.json"),
        "template": meta.get("template", "_template.docx"),
        "source": meta.get("source", "source.docx"),
    }
    for key, value in meta.items():
        if key not in header_attrs and key not in {"source_path"}:
            header_attrs[key] = value
    header_order = ("schema", "format", "styles", "template", "source")
    header = f"<!--@typed {_attrs_text(header_attrs, first=header_order)}-->"
    blocks = [header]
    for paragraph in document.paragraphs:
        if paragraph.inherit:
            marker_attrs = {"id": paragraph.paragraph_id, "inherit": paragraph.inherit}
        else:
            marker_attrs = {"id": paragraph.paragraph_id, "base": paragraph.base_style}
        marker_order = ("id", "base", "inherit")
        marker = f"<!--@p {_attrs_text(marker_attrs, first=marker_order)}-->"
        body = "".join(_node_to_markup(node, paragraph.base_style) for node in merge_adjacent_text(paragraph.nodes))
        mark_line = ""
        if paragraph.mark_revision:
            mark = paragraph.mark_revision
            mark_attrs = {
                "kind": mark["kind"],
                "id": mark["token_id"],
                **mark.get("attrs", {}),
            }
            mark_line = (
                "\n" + f"<!--@mark {_attrs_text(mark_attrs, first=('kind', 'id'))}-->"
            )
        blocks.append(marker + mark_line + ("\n" + body if body else ""))
    for paragraph_id in document.deletions:
        blocks.append(f'<!--@delete id={attr_value(paragraph_id)}-->')
    return "\n\n".join(blocks) + "\n"


_ATTR_RE = re.compile(r'([A-Za-z_:][A-Za-z0-9_.:-]*)=("(?:[^"\\]|\\.)*")')


def parse_attributes(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if not raw:
        return {}
    attrs: dict[str, str] = {}
    cursor = 0
    for match in _ATTR_RE.finditer(raw):
        if raw[cursor:match.start()].strip():
            raise TypedError(f"invalid attribute syntax: {raw[cursor:match.start()].strip()}")
        value = match.group(2)[1:-1]
        value = xml_unescape(value)
        if match.group(1) in attrs:
            raise TypedError(f"duplicate attribute: {match.group(1)}")
        attrs[match.group(1)] = value
        cursor = match.end()
    if raw[cursor:].strip():
        raise TypedError(f"invalid attribute syntax: {raw[cursor:].strip()}")
    return attrs


def _parse_tag(tag: str) -> tuple[str, bool, bool, dict[str, str]]:
    match = re.fullmatch(r"<(/?)([A-Za-z][A-Za-z0-9_.:-]*)(.*?)>", tag)
    if not match:
        raise TypedError(f"malformed typed tag: {tag}")
    closing = bool(match.group(1))
    rest = match.group(3).strip()
    self_closing = rest.endswith("/") and not closing
    if self_closing:
        rest = rest[:-1].rstrip()
    return match.group(2), closing, self_closing, parse_attributes(rest)


def parse_inline(text: str, base_style: str) -> list[Node]:
    nodes: list[Node] = []
    ranges: list[RangeNode] = []
    revisions: list[RevisionNode] = []
    span_style: str | None = None
    cursor = 0

    def append(node: Node) -> None:
        if revisions:
            target = revisions[-1].children
        elif ranges:
            target = ranges[-1].children
        else:
            target = nodes
        target.append(node)

    while cursor < len(text):
        marker = text.find("<", cursor)
        if marker < 0:
            literal = text[cursor:]
            if literal:
                append(TextNode(span_style or base_style, xml_unescape(literal)))
            break
        literal = text[cursor:marker]
        if literal:
            append(TextNode(span_style or base_style, xml_unescape(literal)))
        end = text.find(">", marker + 1)
        if end < 0:
            raise TypedError("unclosed typed tag")
        tag = text[marker:end + 1]
        name, closing, self_closing, attrs = _parse_tag(tag)
        if name == "span":
            if closing:
                if self_closing or span_style is None:
                    raise TypedError("unexpected span close")
                span_style = None
            else:
                if self_closing or span_style is not None or set(attrs) != {"data-s"} or not attrs["data-s"]:
                    raise TypedError("span must be a non-nested <span data-s=...> range")
                span_style = attrs["data-s"]
        elif name == "docx-range":
            if closing:
                if self_closing or not ranges or span_style is not None:
                    raise TypedError("unexpected or unclosed docx-range")
                current = ranges.pop()
                if attrs:
                    raise TypedError("docx-range close cannot have attributes")
                append(current)
            else:
                if self_closing or "id" not in attrs or "kind" not in attrs:
                    raise TypedError("docx-range requires id and kind")
                token_id = attrs.pop("id")
                kind = attrs.pop("kind")
                if not token_id or not kind:
                    raise TypedError("docx-range requires non-empty id and kind")
                ranges.append(RangeNode(token_id, kind, attrs, []))
        elif name in {"docx-anchor", "docx-inline", "docx-opaque"}:
            if closing or not self_closing or "id" not in attrs or "kind" not in attrs:
                raise TypedError(f"{name} must be a self-closing token with id and kind")
            token_id = attrs.pop("id")
            kind = attrs.pop("kind")
            if not token_id or not kind:
                raise TypedError(f"{name} requires non-empty id and kind")
            if name == "docx-anchor":
                append(AnchorNode(token_id, kind, attrs))
            elif name == "docx-inline":
                style = attrs.pop("style", "")
                append(InlineNode(token_id, kind, style, attrs))
            else:
                append(OpaqueNode(token_id, kind, attrs))
        elif name == "docx-revision":
            if closing:
                if self_closing or not revisions or span_style is not None:
                    raise TypedError("unexpected or unclosed docx-revision")
                current = revisions.pop()
                if attrs:
                    raise TypedError("docx-revision close cannot have attributes")
                append(current)
            else:
                if self_closing or "id" not in attrs or "kind" not in attrs:
                    raise TypedError("docx-revision requires id and kind")
                token_id = attrs.pop("id")
                kind = attrs.pop("kind")
                if kind not in {"insert", "delete", "move_from", "move_to"}:
                    raise TypedError(f"unknown revision kind: {kind}")
                revisions.append(RevisionNode(token_id, kind, attrs, []))
        else:
            raise TypedError(f"unknown typed tag: {name}")
        cursor = end + 1
    if span_style is not None or ranges or revisions:
        raise TypedError("unclosed typed structure")
    return merge_adjacent_text(nodes)


def _parse_comment_marker(line: str, prefix: str) -> dict[str, str] | None:
    if not line.startswith(prefix) or not line.endswith("-->"):
        return None
    raw = line[len(prefix):-3].strip()
    return parse_attributes(raw)


def parse_typed(text: str) -> TypedDocument:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not lines[0].startswith("<!--@typed"):
        raise TypedError("typed source must start with a @typed header")
    header_match = re.fullmatch(r"<!--@typed(.*?)-->", lines[0])
    if not header_match:
        raise TypedError("malformed @typed header")
    meta = parse_attributes(header_match.group(1).strip())
    if meta.get("schema") != "1":
        raise TypedError("incompatible typed source schema")
    document = TypedDocument(meta)
    index = 1
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        delete_attrs = _parse_comment_marker(lines[index], "<!--@delete")
        if delete_attrs is not None:
            if set(delete_attrs) != {"id"} or not delete_attrs["id"]:
                raise TypedError("delete marker requires one non-empty id")
            document.deletions.append(delete_attrs["id"])
            index += 1
            continue
        marker_attrs = _parse_comment_marker(lines[index], "<!--@p")
        if marker_attrs is None:
            raise TypedError(f"expected paragraph marker at line {index + 1}")
        if "id" not in marker_attrs or ("base" in marker_attrs) == ("inherit" in marker_attrs):
            raise TypedError("paragraph marker requires exactly one of base or inherit")
        paragraph_id = marker_attrs["id"]
        if not paragraph_id:
            raise TypedError("paragraph ID cannot be empty")
        base_style = marker_attrs.get("base", "")
        inherit = marker_attrs.get("inherit", "")
        index += 1
        mark_revision: dict[str, Any] | None = None
        if index < len(lines) and lines[index].startswith("<!--@mark"):
            mark_attrs = _parse_comment_marker(lines[index], "<!--@mark")
            if mark_attrs is None or "kind" not in mark_attrs or "id" not in mark_attrs:
                raise TypedError("mark marker requires kind and id")
            if mark_attrs["kind"] not in ("insert", "delete"):
                raise TypedError(f"unknown mark kind: {mark_attrs['kind']}")
            mark_revision = {
                "kind": mark_attrs.pop("kind"),
                "token_id": mark_attrs.pop("id"),
                "attrs": mark_attrs,
            }
            index += 1
        body_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            if lines[index].startswith("<!--@p") or lines[index].startswith("<!--@delete"):
                break
            body_lines.append(lines[index])
            index += 1
        if len(body_lines) > 1:
            raise TypedError(f"paragraph {paragraph_id} must use one logical source line")
        body = body_lines[0] if body_lines else ""
        document.paragraphs.append(
            Paragraph(
                paragraph_id,
                base_style,
                parse_inline(body, base_style),
                inherit=inherit,
                mark_revision=mark_revision,
            )
        )
    paragraph_ids = {paragraph.paragraph_id for paragraph in document.paragraphs}
    if len(paragraph_ids) != len(document.paragraphs):
        raise TypedError("duplicate paragraph ID")
    if len(set(document.deletions)) != len(document.deletions):
        raise TypedError("duplicate deletion tombstone")
    if paragraph_ids.intersection(document.deletions):
        raise TypedError("paragraph cannot be both live and deleted")
    return document


def _project_nodes(nodes: Iterable[Node], *, base_style: str, style_labels: dict[str, str] | None, styled: bool) -> str:
    values: list[str] = []
    for node in nodes:
        if isinstance(node, TextNode):
            if styled:
                label = (style_labels or {}).get(node.style_id, node.style_id)
                values.append(f"[{node.style_id}:{label}]{node.text}[/{node.style_id}]")
            else:
                values.append(node.text)
        elif isinstance(node, RangeNode):
            values.append(_project_nodes(node.children, base_style=base_style, style_labels=style_labels, styled=styled))
        elif isinstance(node, RevisionNode):
            if node.kind in ("insert", "move_to"):
                values.append(
                    _project_nodes(node.children, base_style=base_style, style_labels=style_labels, styled=styled)
                )
            # deletions are hidden in the final-view projections
        elif isinstance(node, InlineNode):
            values.append({"tab": "\t", "br": "\n", "cr": "\n"}.get(node.kind, f"[{node.kind}]"))
        elif isinstance(node, OpaqueNode):
            values.append(f"[opaque:{node.kind}]")
    return "".join(values)


def project_clean(document: TypedDocument, *, markers: bool = True) -> str:
    blocks: list[str] = []
    for paragraph in document.paragraphs:
        body = _project_nodes(paragraph.nodes, base_style=paragraph.base_style, style_labels=None, styled=False)
        blocks.append(f"--- {paragraph.paragraph_id} ---\n{body}" if markers else body)
    return ("\n\n" if markers else "\n").join(blocks)


def project_style(document: TypedDocument, styles: StyleRegistry, *, markers: bool = True) -> str:
    blocks: list[str] = []
    labels = {style_id: style.label for style_id, style in styles.styles.items()}
    for paragraph in document.paragraphs:
        body = _project_nodes(paragraph.nodes, base_style=paragraph.base_style, style_labels=labels, styled=True)
        blocks.append(f"--- {paragraph.paragraph_id} ---\n{body}" if markers else body)
    return ("\n\n" if markers else "\n").join(blocks)


# --------------------------------------------------------------------------
# Edit mode (ADR 0037: three-field state)
# --------------------------------------------------------------------------

def effective_edit_mode(
    *,
    source_track_enabled: bool,
    has_pending_revisions: bool,
    explicit: str | None = None,
) -> str:
    """Effective edit mode from the three-field state.

    ``explicit`` is a user override (``track``/``direct``). Otherwise the
    mode follows the signals: both on -> track, both off -> direct, one
    without the other -> ambiguous (an audit document is never silently
    edited in place).
    """
    if explicit is not None:
        if explicit not in ("track", "direct"):
            raise ValueError(f"invalid edit mode: {explicit}")
        return explicit
    if source_track_enabled and has_pending_revisions:
        return "track"
    if not source_track_enabled and not has_pending_revisions:
        return "direct"
    return "ambiguous"


# --------------------------------------------------------------------------
# Revision decisions (ADR 0037 R3)
# --------------------------------------------------------------------------

def revision_fingerprint(node: RevisionNode) -> str:
    """Content fingerprint for a revision node (matches the inventory key)."""
    text = "".join(child.text for child in node.children if isinstance(child, TextNode))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def find_revision(paragraph: Paragraph, w_id: str) -> RevisionNode | None:
    """Locate a revision node by w:id (unique package-wide)."""
    def walk(nodes: Iterable[Node]) -> RevisionNode | None:
        for node in nodes:
            if isinstance(node, RevisionNode):
                if node.attrs.get("w:id") == w_id:
                    return node
                hit = walk(node.children)
                if hit is not None:
                    return hit
            elif isinstance(node, RangeNode):
                hit = walk(node.children)
                if hit is not None:
                    return hit
        return None

    return walk(paragraph.nodes)


def _locate_revision(paragraph: Paragraph, w_id: str) -> tuple[RevisionNode, list[Node], int] | None:
    """(node, containing list, index) for mutation."""
    def walk(nodes: list[Node]) -> tuple[RevisionNode, list[Node], int] | None:
        for index, node in enumerate(nodes):
            if isinstance(node, RevisionNode):
                if node.attrs.get("w:id") == w_id:
                    return node, nodes, index
                hit = walk(node.children)
                if hit is not None:
                    return hit
            elif isinstance(node, RangeNode):
                hit = walk(node.children)
                if hit is not None:
                    return hit
        return None

    return walk(paragraph.nodes)


def _revision_text(node: RevisionNode) -> str:
    return "".join(child.text for child in node.children if isinstance(child, TextNode))


def apply_revision_decision(
    paragraph: Paragraph,
    *,
    w_id: str,
    kind: str,
    fingerprint: str,
    action: str,
) -> dict[str, Any]:
    """Apply one accept/reject decision to a paragraph (pure AST mutation).

    Tree semantics (ADR 0037): accept insert = unwrap children; reject
    insert = delete node; accept delete = delete node; reject delete =
    unwrap children. Unwrapping an outer revision keeps inner ones; deleting
    an outer deletes its inner ones. Returns a change record.
    """
    import hashlib

    if action not in ("accept", "reject"):
        raise ValueError(f"unknown decision action: {action}")
    located = _locate_revision(paragraph, w_id)
    if located is None:
        raise KeyError(f"revision not found: w:id {w_id}")
    node, container, index = located
    if node.kind != kind:
        raise ValueError(
            f"revision kind mismatch: key says {kind}, node is {node.kind}"
        )
    actual = hashlib.sha256(_revision_text(node).encode("utf-8")).hexdigest()[:12]
    if actual != fingerprint:
        raise ValueError(
            f"revision-text-fingerprint-mismatch: expected {fingerprint}, got {actual}"
        )
    remove = (action == "accept" and node.kind in ("delete", "move_from")) or (
        action == "reject" and node.kind in ("insert", "move_to")
    )
    if remove:
        del container[index]
    else:
        container[index : index + 1] = node.children
    return {
        "w_id": w_id,
        "kind": node.kind,
        "action": action,
        "fingerprint": fingerprint,
        "paragraph_id": paragraph.paragraph_id,
        "operation": "remove" if remove else "unwrap",
    }


def reinsert_deleted_text(
    paragraph: Paragraph,
    *,
    w_id: str,
    fingerprint: str,
    token_id: str,
    attrs: dict[str, str],
    text: str | None = None,
) -> RevisionNode:
    """Create a NEW insertion revision after an existing deletion, without
    touching the original deletion (ADR 0037). ``text`` defaults to the
    deleted text."""
    import hashlib

    located = _locate_revision(paragraph, w_id)
    if located is None:
        raise KeyError(f"revision not found: w:id {w_id}")
    node, container, index = located
    if node.kind not in ("delete", "move_from"):
        raise ValueError(f"reinsert target is not a deletion: {node.kind}")
    actual = hashlib.sha256(_revision_text(node).encode("utf-8")).hexdigest()[:12]
    if actual != fingerprint:
        raise ValueError(
            f"revision-text-fingerprint-mismatch: expected {fingerprint}, got {actual}"
        )
    if text is None:
        text = _revision_text(node)
    style = node.children[0].style_id if node.children and isinstance(node.children[0], TextNode) else paragraph.base_style
    new_node = RevisionNode(token_id, "insert", dict(attrs), [TextNode(style, text)])
    container.insert(index + 1, new_node)
    return new_node


def apply_all_decisions(
    paragraphs: Iterable[Paragraph],
    action: str,
) -> tuple[list[Paragraph], list[dict[str, Any]]]:
    """Accept or reject every revision in the document (used by accept-all /
    reject-all new-baseline flows). Returns (transformed paragraphs, change
    records)."""
    import hashlib

    transformed: list[Paragraph] = []
    changes: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        changed_paragraph = Paragraph(
            paragraph.paragraph_id,
            paragraph.base_style,
            list(paragraph.nodes),
            p_open=paragraph.p_open,
            ppr=paragraph.ppr,
            raw_xml=paragraph.raw_xml,
            section_bearing=paragraph.section_bearing,
            editable=paragraph.editable,
            inherit=paragraph.inherit,
            original_index=paragraph.original_index,
            # accept-all/reject-all settle every revision: paragraph marks are
            # resolved too, so the new baseline carries no revision state.
            mark_revision=None,
        )
        if not paragraph.editable or contains_opaque(changed_paragraph.nodes):
            # paragraphs with unsupported structure replay untouched; their
            # revisions stay out of the decided baseline
            transformed.append(changed_paragraph)
            continue
        while True:
            target = _first_revision(changed_paragraph.nodes)
            if target is None:
                break
            w_id = target.attrs.get("w:id", "")
            changes.append(
                apply_revision_decision(
                    changed_paragraph,
                    w_id=w_id,
                    kind=target.kind,
                    fingerprint=hashlib.sha256(_revision_text(target).encode("utf-8")).hexdigest()[:12],
                    action=action,
                )
            )
        changed_paragraph.nodes = merge_adjacent_text(changed_paragraph.nodes)
        transformed.append(changed_paragraph)
    return transformed, changes


def _first_revision(nodes: list[Node]) -> RevisionNode | None:
    for candidate in nodes:
        if isinstance(candidate, RevisionNode):
            return candidate
        if isinstance(candidate, (RangeNode, RevisionNode)):
            hit = _first_revision(candidate.children)
            if hit is not None:
                return hit
    return None
