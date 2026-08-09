"""Generate a self-contained document review console.

The console is intentionally a reader-first surface: the document stays in a
paper-like stage, review items live in a sticky academic index, and technical
style diagnostics are opt-in. Rendering uses the canonical typed AST rather
than a regular-expression projection, so nested spans, ranges, anchors and
tracked changes keep their structure.

Usage: python -m scripts.review_console <workdir> -o <out.html>
"""
from __future__ import annotations

import argparse
import html
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

try:
    from .typed_core import (
        AnchorNode,
        InlineNode,
        Node,
        OpaqueNode,
        RangeNode,
        RevisionNode,
        TextNode,
        parse_typed,
        rpr_features,
    )
except ImportError:  # pragma: no cover - direct script invocation fallback
    from typed_core import (  # type: ignore[no-redef]
        AnchorNode,
        InlineNode,
        Node,
        OpaqueNode,
        RangeNode,
        RevisionNode,
        TextNode,
        parse_typed,
        rpr_features,
    )

_ESCAPE = html.escape
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _w(name: str) -> str:
    return f"{{{_W_NS}}}{name}"




_HIGHLIGHT_HEX = {
    "black": "#000000",
    "blue": "#0000FF",
    "cyan": "#00FFFF",
    "green": "#00FF00",
    "magenta": "#FF00FF",
    "red": "#FF0000",
    "yellow": "#FFFF00",
    "white": "#FFFFFF",
    "darkBlue": "#00008B",
    "darkCyan": "#008B8B",
    "darkGreen": "#006400",
    "darkMagenta": "#8B008B",
    "darkRed": "#8B0000",
    "darkYellow": "#808000",
    "darkGray": "#A9A9A9",
    "lightGray": "#D3D3D3",
}

# These are the only interface colors. Source document colors are data-derived
# from the Word style registry and therefore do not alter the chrome palette.
_UI_COLORS = {
    "canvas": "#F1F0EC",
    "paper": "#FBFAF7",
    "ink": "#111111",
    "ink-muted": "#686761",
    "hairline": "#D7D5CE",
    "hairline-soft": "#E7E5DE",
    "signal": "#E34234",
    "signal-dark": "#A9241B",
    "cobalt": "#1646B8",
    "insert-wash": "#E6EEF9",
    "delete-wash": "#F9E7E4",
    "comment-wash": "#F5E9C7",
    "success": "#16704A",
    "warning": "#8A5A00",
}


@dataclass
class StyleInfo:
    css: dict[str, str]
    attrs: dict[str, str]
    mapped: list[str]
    unmapped: list[str]
    label: str = "normal"


def _css_font(value: str) -> str:
    # Font names come from the local DOCX. Strip quote characters so a malformed
    # source name cannot break the generated style rule.
    cleaned = value.replace("'", "").replace('"', "").strip()
    return f"'{cleaned}'" if cleaned else ""


def _number(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _raw_rpr_color(style_rec: dict[str, Any], theme_colors: dict[str, str]) -> str | None:
    raw = str(style_rec.get("rPr", ""))
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    color = root.find(_w("color"))
    if color is None:
        return None
    theme_name = color.attrib.get(_w("themeColor"))
    if theme_name and theme_name in theme_colors:
        return theme_colors[theme_name]
    value = color.attrib.get(_w("val"))
    if value and value.lower() not in {"auto", "none"}:
        return value
    return None


def _add_decoration(css: dict[str, str], value: str) -> None:
    existing = css.get("text-decoration", "")
    if value not in existing:
        css["text-decoration"] = f"{existing} {value}".strip()


def rpr_to_css(
    style_rec: dict[str, Any],
    *,
    theme_colors: dict[str, str] | None = None,
) -> StyleInfo:
    """Map canonical Word run properties to CSS and report omissions.

    The renderer handles visible properties from the full typed_core feature
    vocabulary. Non-visual hints are counted as understood; truly unsupported
    visual effects remain visible in the opt-in diagnostics panel.
    """
    features = dict(style_rec.get("features") or {})
    theme_colors = theme_colors or {}
    css: dict[str, str] = {}
    attrs: dict[str, str] = {}
    mapped: set[str] = set()
    unmapped: list[str] = []

    fonts = [
        _css_font(str(features.get(key, "")))
        for key in ("font:ascii", "font:hAnsi", "font:eastAsia", "font:cs")
        if features.get(key)
    ]
    if fonts:
        css["font-family"] = ", ".join(dict.fromkeys(fonts + ["Arial", "sans-serif"]))
        mapped.update(key for key in features if key.startswith("font:"))

    size = _number(features.get("sz"))
    if size is not None:
        css["font-size"] = f"{size / 2:g}pt"
        mapped.add("sz")
    size_cs = _number(features.get("szCs"))
    if size is None and size_cs is not None:
        css["font-size"] = f"{size_cs / 2:g}pt"
    if size_cs is not None:
        mapped.add("szCs")

    if features.get("b"):
        css["font-weight"] = "700"
        mapped.add("b")
    if features.get("i"):
        css["font-style"] = "italic"
        mapped.add("i")
    if features.get("strike"):
        _add_decoration(css, "line-through")
        mapped.add("strike")
    if features.get("dstrike"):
        _add_decoration(css, "line-through")
        css["text-decoration-style"] = "double"
        mapped.add("dstrike")
    if features.get("smallCaps"):
        css["font-variant-caps"] = "small-caps"
        mapped.add("smallCaps")
    if features.get("caps"):
        css["text-transform"] = "uppercase"
        mapped.add("caps")
    if features.get("outline"):
        css["-webkit-text-stroke"] = "0.25px currentColor"
        mapped.add("outline")
    if features.get("shadow"):
        css["text-shadow"] = "1px 1px 0 rgba(17,17,17,.24)"
        mapped.add("shadow")
    if features.get("emboss"):
        css["text-shadow"] = "1px 1px 0 rgba(255,255,255,.75), -1px -1px 0 rgba(17,17,17,.22)"
        mapped.add("emboss")
    if features.get("imprint"):
        css["text-shadow"] = "-1px -1px 0 rgba(255,255,255,.75), 1px 1px 0 rgba(17,17,17,.22)"
        mapped.add("imprint")
    if features.get("vanish") or features.get("webHidden"):
        css["opacity"] = ".42"
        css["text-decoration"] = "underline dotted"
        mapped.update(key for key in ("vanish", "webHidden") if key in features)

    vertical = str(features.get("vertAlign", ""))
    if vertical in {"superscript", "subscript"}:
        css["vertical-align"] = "super" if vertical == "superscript" else "sub"
        css["font-size"] = ".72em"
        mapped.add("vertAlign")
    position = _number(features.get("position"))
    if position is not None:
        mapped.add("position")
        if not vertical:
            css["vertical-align"] = f"{position / 2:g}pt"

    color = _raw_rpr_color(style_rec, theme_colors) or str(features.get("color", ""))
    if color:
        color_l = color.lstrip("#")
        if re.fullmatch(r"[0-9A-Fa-f]{6}", color_l):
            css["color"] = f"#{color_l}"
            mapped.add("color")
        elif color.lower() == "auto":
            mapped.add("color")
        else:
            unmapped.append(f"color={color}")
    highlight = str(features.get("highlight", ""))
    if highlight:
        if highlight in _HIGHLIGHT_HEX:
            css["background-color"] = _HIGHLIGHT_HEX[highlight]
            mapped.add("highlight")
        elif highlight == "none":
            mapped.add("highlight")
        else:
            unmapped.append(f"highlight={highlight}")

    underline = str(features.get("u", ""))
    underline_styles = {
        "single": "solid",
        "words": "solid",
        "double": "double",
        "dotted": "dotted",
        "dottedHeavy": "dotted",
        "dash": "dashed",
        "dashed": "dashed",
        "dashLong": "dashed",
        "dashLongHeavy": "dashed",
        "dashDotHeavy": "dashed",
        "dashDotDotHeavy": "dashed",
        "wave": "wavy",
        "wavyHeavy": "wavy",
        "wavyDouble": "wavy",
    }
    if underline:
        if underline in {"none", "false"}:
            mapped.add("u")
        elif underline in underline_styles:
            _add_decoration(css, "underline")
            css["text-decoration-style"] = underline_styles[underline]
            mapped.add("u")
        else:
            unmapped.append(f"u={underline}")

    kern = _number(features.get("kern"))
    if kern is not None:
        css["letter-spacing"] = f"{kern / 2:g}pt"
        mapped.add("kern")
    spacing = _number(features.get("spacing"))
    if spacing is not None:
        css["letter-spacing"] = f"{spacing / 20:g}pt"
        mapped.add("spacing")
    width = _number(features.get("w"))
    if width is not None:
        css["font-stretch"] = f"{max(50, min(200, width)):g}%"
        mapped.add("w")

    if features.get("rtl"):
        css["direction"] = "rtl"
        css["unicode-bidi"] = "embed"
        mapped.add("rtl")
    language = str(features.get("lang", ""))
    if language:
        attrs["lang"] = language.replace("_", "-")
        mapped.add("lang")
    emphasis = str(features.get("em", ""))
    if emphasis:
        css["text-emphasis"] = "filled dot" if emphasis in {"true", "1"} else f"filled {emphasis}"
        mapped.add("em")

    # These properties affect selection/inheritance rather than a visible glyph.
    for key in ("font:hint", "rStyle", "cs"):
        if key in features:
            mapped.add(key)

    for key in features:
        if key not in mapped and not key.startswith("font:"):
            unmapped.append(key)
    return StyleInfo(
        css=css,
        attrs=attrs,
        mapped=sorted(mapped),
        unmapped=sorted(set(unmapped)),
        label=str(style_rec.get("label") or "normal"),
    )


# ------------------------------------------------------------- template reads


def _template_parts(workdir: Path) -> dict[str, bytes]:
    template = workdir / "_template.docx"
    with zipfile.ZipFile(template) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _theme_colors(parts: dict[str, bytes]) -> dict[str, str]:
    raw = parts.get("word/theme/theme1.xml", b"")
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    scheme = root.find(f".//{{{_A_NS}}}clrScheme")
    if scheme is None:
        return {}
    result: dict[str, str] = {}
    for entry in list(scheme):
        if not entry.tag.startswith("{" + _A_NS + "}"):
            continue
        name = entry.tag.rsplit("}", 1)[-1]
        child = next(iter(entry), None)
        if child is None:
            continue
        value = child.attrib.get("val") or child.attrib.get("lastClr")
        if value and re.fullmatch(r"[0-9A-Fa-f]{6}", value):
            result[name] = f"#{value}"
    return result


def _rstyle_features(parts: dict[str, bytes]) -> dict[str, dict[str, str]]:
    raw = parts.get("word/styles.xml", b"")
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    definitions: dict[str, tuple[str, dict[str, Any]]] = {}
    for style in root.findall(_w("style")):
        if style.attrib.get(_w("type")) != "character":
            continue
        style_id = style.attrib.get(_w("styleId"), "")
        if not style_id:
            continue
        parent = style.find(_w("basedOn"))
        parent_id = parent.attrib.get(_w("val"), "") if parent is not None else ""
        rpr = style.find(_w("rPr"))
        own = rpr_features(ET.tostring(rpr, encoding="unicode")) if rpr is not None else {}
        definitions[style_id] = (parent_id, own)

    resolved: dict[str, dict[str, str]] = {}

    def resolve(style_id: str, trail: set[str] | None = None) -> dict[str, str]:
        if style_id in resolved:
            return resolved[style_id]
        trail = trail or set()
        if style_id in trail or style_id not in definitions:
            return {}
        parent, own = definitions[style_id]
        values = dict(resolve(parent, trail | {style_id}))
        values.update({key: str(value) for key, value in own.items()})
        resolved[style_id] = values
        return values

    for style_id in definitions:
        resolve(style_id)
    return resolved


def _comments_meta(parts: dict[str, bytes]) -> dict[str, dict[str, str]]:
    raw = parts.get("word/comments.xml", b"")
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    result: dict[str, dict[str, str]] = {}
    for comment in root.findall(f".//{_w('comment')}"):
        comment_id = comment.attrib.get(_w("id"))
        if comment_id is None:
            continue
        result[comment_id] = {
            "author": comment.attrib.get(_w("author"), ""),
            "date": comment.attrib.get(_w("date"), ""),
            "text": "".join(comment.itertext()).strip(),
        }
    return result


def _revision_keys(workdir: Path) -> dict[str, str]:
    try:
        data = json.loads((workdir / "revisions.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    values = data.get("revisions", [])
    return {
        str(rev.get("w_id")): str(rev.get("revision_key", ""))
        for rev in values
        if isinstance(rev, dict) and rev.get("w_id") is not None
    }


def _load_styles(
    workdir: Path,
    parts: dict[str, bytes],
) -> tuple[dict[str, dict[str, Any]], dict[str, StyleInfo], dict[str, str]]:
    raw_styles = json.loads((workdir / "styles.json").read_text(encoding="utf-8"))["styles"]
    styles: dict[str, dict[str, Any]] = {
        sid: {**rec, "features": dict(rec.get("features") or {})}
        for sid, rec in raw_styles.items()
    }
    rstyles = _rstyle_features(parts)
    for rec in styles.values():
        rstyle_id = rec["features"].get("rStyle")
        if rstyle_id and rstyle_id in rstyles:
            expanded = dict(rstyles[rstyle_id])
            expanded.update(rec["features"])
            rec["features"] = expanded
    theme_colors = _theme_colors(parts)
    infos = {
        sid: rpr_to_css(rec, theme_colors=theme_colors)
        for sid, rec in styles.items()
    }
    return styles, infos, theme_colors


# ------------------------------------------------------------------ renderer


@dataclass
class RenderContext:
    style_info: dict[str, StyleInfo]
    comments_meta: dict[str, dict[str, str]]
    revision_keys: dict[str, str]
    revision_records: list[dict[str, str]] = field(default_factory=list)
    comment_records: list[dict[str, str]] = field(default_factory=list)
    _revision_seen: set[str] = field(default_factory=set)
    _comment_seen: set[str] = field(default_factory=set)
    paragraph_id: str = ""


def _node_text(nodes: Iterable[Node]) -> str:
    result: list[str] = []
    for node in nodes:
        if isinstance(node, TextNode):
            result.append(node.text)
        elif isinstance(node, (RangeNode, RevisionNode)):
            result.append(_node_text(node.children))
        elif isinstance(node, InlineNode):
            if node.kind == "tab":
                result.append("\t")
            elif node.kind in {"br", "cr"}:
                result.append("\n")
        # Opaque and unsupported inline nodes are structural data, not prose.
    return "".join(result)


def _clip(text: str, length: int = 74) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    if len(value) <= length:
        return value
    return value[: length - 1] + "…"


def _date_label(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y.%m.%d")
    except ValueError:
        return value[:10]


def _attr(name: str, value: str) -> str:
    return f'{name}="{_ESCAPE(value, quote=True)}"'


def _record_revision(node: RevisionNode, ctx: RenderContext) -> dict[str, str]:
    if node.token_id not in ctx._revision_seen:
        attrs = node.attrs
        w_id = attrs.get("w:id", "")
        record = {
            "rid": node.token_id,
            "wid": w_id,
            "key": ctx.revision_keys.get(w_id, "") or node.token_id,
            "kind": node.kind,
            "author": attrs.get("w:author", ""),
            "date": attrs.get("w:date", ""),
            "text": _node_text(node.children),
            "pid": ctx.paragraph_id,
            "order": str(len(ctx.revision_records) + 1),
        }
        ctx.revision_records.append(record)
        ctx._revision_seen.add(node.token_id)
        return record
    return next(record for record in ctx.revision_records if record["rid"] == node.token_id)


def _record_comment(node: AnchorNode, ctx: RenderContext) -> dict[str, str] | None:
    if node.kind != "comment-start":
        return None
    comment_id = node.attrs.get("w:id", "")
    if not comment_id:
        return None
    if comment_id not in ctx._comment_seen:
        meta = ctx.comments_meta.get(comment_id, {})
        record = {
            "cid": comment_id,
            "author": meta.get("author", ""),
            "date": meta.get("date", ""),
            "text": meta.get("text", ""),
            "pid": ctx.paragraph_id,
            "order": str(len(ctx.comment_records) + 1),
            "source": "word",
        }
        ctx.comment_records.append(record)
        ctx._comment_seen.add(comment_id)
        return record
    return next(record for record in ctx.comment_records if record["cid"] == comment_id)


def _render_nodes(nodes: Iterable[Node], ctx: RenderContext) -> str:
    return "".join(_render_node(node, ctx) for node in nodes)


def _render_node(node: Node, ctx: RenderContext) -> str:
    if isinstance(node, TextNode):
        info = ctx.style_info.get(node.style_id)
        classes = f"source-text s-{_ESCAPE(node.style_id, quote=True)}"
        lang = _attr("lang", info.attrs["lang"]) + " " if info and info.attrs.get("lang") else ""
        return f'<span class="{classes}" {lang}>{_ESCAPE(node.text)}</span>'

    if isinstance(node, RevisionNode):
        record = _record_revision(node, ctx)
        kind_class = {
            "delete": "delete",
            "insert": "insert",
            "move_from": "move-from",
            "move_to": "move-to",
        }.get(node.kind, "other")
        label = {
            "delete": "删除",
            "insert": "插入",
            "move_from": "移出",
            "move_to": "移入",
        }.get(node.kind, "修订")
        attrs = " ".join(
            [
                _attr("id", f"revision-{node.token_id}"),
                _attr("class", f"revision-mark revision-{kind_class}"),
                _attr("data-rid", node.token_id),
                _attr("data-kind", node.kind),
                _attr("data-label", label),
                "role=\"button\"",
                "tabindex=\"0\"",
                _attr("aria-label", f"{label}修订：{_clip(record['text'], 40)}"),
            ]
        )
        inner = _render_nodes(node.children, ctx)
        return f'<span {attrs}><span class="revision-content">{inner}</span></span>'

    if isinstance(node, AnchorNode):
        record = _record_comment(node, ctx)
        if record is not None:
            number = record["order"].zfill(2)
            label = f"批注 {number}"
            return (
                f'<button class="comment-anchor" {_attr("data-cid", record["cid"])} '
                f'{_attr("aria-label", label)} {_attr("title", label)}>{number}</button>'
            )
        # Bookmarks and structural anchors remain in the typed model but do not
        # interrupt the reader's line of sight.
        return '<span class="structural-anchor" aria-hidden="true"></span>'

    if isinstance(node, InlineNode):
        if node.kind == "commentReference":
            return ""
        if node.kind == "tab":
            return '<span class="inline-tab" aria-hidden="true"></span>'
        if node.kind in {"br", "cr"}:
            return "<br>"
        if node.kind == "lastRenderedPageBreak":
            return '<span class="page-break" role="separator" aria-label="原文页分隔"></span>'
        if node.kind in {"footnoteReference", "endnoteReference"}:
            return '<sup class="reference-marker" aria-label="脚注引用">†</sup>'
        return ""

    if isinstance(node, OpaqueNode):
        return ""

    if isinstance(node, RangeNode):
        range_class = " source-range-link" if "hyperlink" in node.kind.lower() else ""
        return f'<span class="source-range{range_class}">{_render_nodes(node.children, ctx)}</span>'

    return ""


def _paragraph_html(paragraph: Any, ctx: RenderContext) -> str:
    ctx.paragraph_id = paragraph.paragraph_id
    classes = ["document-paragraph"]
    if paragraph.section_bearing:
        classes.append("paragraph-section")
    body = _render_nodes(paragraph.nodes, ctx)
    if not body.strip():
        return ""
    return (
        f'<p class="{" ".join(classes)}" {_attr("data-pid", paragraph.paragraph_id)}>'
        f"{body}</p>"
    )


def _format_audit_rows(styles: dict[str, dict[str, Any]], infos: dict[str, StyleInfo]) -> tuple[str, int]:
    rows: list[str] = []
    warning_count = 0
    for sid in sorted(styles):
        info = infos[sid]
        if info.unmapped:
            warning_count += 1
        mapped = ", ".join(info.mapped) or "—"
        unmapped = ", ".join(info.unmapped) or "—"
        state = "warning" if info.unmapped else "ok"
        rows.append(
            "<tr>"
            f"<td class=\"mono\">{_ESCAPE(sid)}</td>"
            f"<td>{_ESCAPE(info.label)}</td>"
            f"<td class=\"audit-{state}\">{_ESCAPE(mapped)}</td>"
            f"<td class=\"audit-{state}\">{_ESCAPE(unmapped)}</td>"
            "</tr>"
        )
    return "".join(rows), warning_count


def _review_item_html(record: dict[str, str]) -> str:
    label = {
        "delete": "删除",
        "insert": "插入",
        "move_from": "移出",
        "move_to": "移入",
    }.get(record["kind"], "修订")
    meta = " · ".join(value for value in (record.get("author", ""), _date_label(record.get("date", ""))) if value)
    return (
        f'<button class="review-item" {_attr("data-rid", record["rid"])} '
        f'data-status="pending" aria-label="{_ESCAPE(label + "：" + _clip(record["text"], 44), quote=True)}">'
        f'<span class="review-index">{_ESCAPE(record["order"])}</span>'
        f'<span class="review-item-copy"><span class="review-item-label">{label}</span>'
        f'<span class="review-item-quote">{_ESCAPE(_clip(record["text"]))}</span>'
        f'<span class="review-item-meta">{_ESCAPE(meta or "未标注作者")}</span></span>'
        "</button>"
    )


def _comment_item_html(record: dict[str, str]) -> str:
    meta = " · ".join(value for value in (record.get("author", ""), _date_label(record.get("date", ""))) if value)
    return (
        f'<button class="comment-item" data-cid={_ESCAPE(record["cid"], quote=True)!r} '
        f'aria-label="批注 {record["order"]}">'
        f'<span class="review-index">{_ESCAPE(record["order"])}</span>'
        f'<span class="review-item-copy"><span class="review-item-label">批注</span>'
        f'<span class="review-item-quote">{_ESCAPE(_clip(record["text"]))}</span>'
        f'<span class="review-item-meta">{_ESCAPE(meta or "未标注作者")}</span></span>'
        "</button>"
    )


def _css(style_rules: list[str]) -> str:
    c = _UI_COLORS
    return f"""
:root {{
  --canvas: {c['canvas']}; --paper: {c['paper']}; --ink: {c['ink']}; --ink-muted: {c['ink-muted']};
  --hairline: {c['hairline']}; --hairline-soft: {c['hairline-soft']}; --signal: {c['signal']};
  --signal-dark: {c['signal-dark']}; --cobalt: {c['cobalt']}; --insert-wash: {c['insert-wash']};
  --delete-wash: {c['delete-wash']}; --comment-wash: {c['comment-wash']}; --success: {c['success']};
  --warning: {c['warning']}; --topbar-height: 0px;
  color-scheme: light;
}}
*, *::before, *::after {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{ margin: 0; background: var(--canvas); color: var(--ink); font-family: Arial, Helvetica, sans-serif; user-select: none; -webkit-user-select: none; }}
button, textarea, select {{ font: inherit; }}
textarea {{ user-select: text; -webkit-user-select: text; }}
button {{ color: inherit; }}
button:focus-visible, textarea:focus-visible, select:focus-visible, summary:focus-visible {{ outline: 2px solid var(--cobalt); outline-offset: 3px; }}
.page-frame {{ width: min(calc(100% - 32px), 1440px); margin: 0 auto; }}
.topbar {{ position: sticky; top: 0; z-index: 30; background: var(--canvas); }}
.console-header {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 32px; align-items: end; padding: 28px 0 22px; border-bottom: 1px solid var(--ink); }}
.brand-overline, .eyebrow {{ margin: 0 0 8px; color: var(--signal); font: 700 11px/1.25 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .12em; text-transform: uppercase; }}
.console-header h1 {{ margin: 0; font-size: clamp(28px, 4vw, 48px); line-height: 1; letter-spacing: -.045em; }}
.console-header p {{ margin: 12px 0 0; color: var(--ink-muted); font-size: 13px; line-height: 1.45; }}
.header-actions {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: end; }}
.view-switch {{ display: inline-flex; border: 1px solid var(--ink); background: var(--paper); }}
.view-button, .primary-action, .rail-tab, .filter-button {{ border: 0; background: transparent; cursor: pointer; }}
.view-button {{ min-height: 36px; padding: 0 12px; border-right: 1px solid var(--hairline); font-size: 12px; }}
.view-button:last-child {{ border-right: 0; }}
.view-button[aria-pressed="true"] {{ background: var(--ink); color: var(--paper); }}
.primary-action {{ min-height: 36px; padding: 0 16px; background: var(--signal); color: var(--paper); font-size: 12px; font-weight: 700; letter-spacing: .01em; }}
.send-action {{ min-height: 36px; padding: 0 14px; border: 1px solid var(--ink); background: var(--paper); color: var(--ink); cursor: pointer; font-size: 12px; font-weight: 700; }}
.send-action:hover {{ background: var(--ink); color: var(--paper); }}
.send-action:disabled {{ cursor: not-allowed; opacity: .45; }}
.server-status {{ color: var(--ink-muted); font: 700 10px/1.2 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .03em; white-space: nowrap; }}
.server-status[data-state="error"] {{ color: var(--signal-dark); }}
.primary-action:hover {{ background: var(--signal-dark); }}
.header-rule {{ display: flex; justify-content: space-between; gap: 16px; padding: 10px 0 12px; border-bottom: 1px solid var(--hairline); color: var(--ink-muted); font-size: 12px; }}
.header-rule strong {{ color: var(--ink); font-weight: 700; }}
.workspace {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 360px); gap: 40px; align-items: start; padding: 32px 0 72px; }}
.document-stage {{ min-width: 0; }}
.stage-heading {{ padding: 0 0 20px; border-bottom: 1px solid var(--hairline); }}
.stage-heading h2 {{ margin: 0; font-size: 22px; letter-spacing: -.025em; line-height: 1.15; }}
.stage-heading p {{ margin: 8px 0 0; color: var(--ink-muted); font-size: 13px; }}
.document-paper {{ margin-top: 20px; padding: clamp(28px, 5vw, 64px) clamp(20px, 6vw, 84px) 72px; background: var(--paper); border: 1px solid var(--hairline); border-top: 4px solid var(--ink); box-shadow: 0 10px 28px rgba(17,17,17,.05); user-select: text; -webkit-user-select: text; }}
.document-paragraph {{ margin: 0 0 18px; min-height: 1.75em; overflow-wrap: anywhere; word-break: break-word; white-space: break-spaces; font-size: 16px; line-height: 1.75; }}
.document-paragraph:last-child {{ margin-bottom: 0; }}
.source-text {{ display: inline; overflow-wrap: anywhere; word-break: break-word; }}
.source-range-link {{ text-decoration: underline; text-decoration-color: var(--cobalt); text-underline-offset: 3px; }}
.revision-mark {{ display: inline; cursor: pointer; border-bottom: 2px solid currentColor; border-radius: 2px; padding: 1px 2px; transition: background-color 120ms ease-out, outline-color 120ms ease-out; }}
.revision-delete, .revision-move-from {{ color: var(--signal-dark); background: var(--delete-wash); text-decoration: line-through; text-decoration-thickness: 1.5px; }}
.revision-insert, .revision-move-to {{ color: var(--cobalt); background: var(--insert-wash); }}
.revision-mark:hover, .revision-mark:focus-visible {{ outline: 2px solid currentColor; outline-offset: 2px; }}
.revision-mark.is-active {{ outline: 2px solid var(--ink); outline-offset: 2px; }}
body[data-view="final"] .revision-delete, body[data-view="final"] .revision-move-from {{ display: none; }}
body[data-view="final"] .revision-insert, body[data-view="final"] .revision-move-to {{ color: inherit; background: transparent; border-bottom: 0; padding: 0; }}
body[data-view="original"] .revision-insert, body[data-view="original"] .revision-move-to {{ display: none; }}
body[data-view="original"] .revision-delete, body[data-view="original"] .revision-move-from {{ color: inherit; background: transparent; border-bottom: 0; padding: 0; text-decoration: none; }}
.comment-anchor {{ display: inline-flex; align-items: center; justify-content: center; min-width: 20px; height: 18px; margin: 0 3px; padding: 0 4px; border: 0; border-bottom: 2px solid var(--warning); background: var(--comment-wash); color: var(--warning); cursor: pointer; font: 700 10px/1 ui-monospace, SFMono-Regular, Consolas, monospace; vertical-align: 2px; }}
.comment-anchor--agent {{ border-bottom-color: var(--cobalt); background: var(--insert-wash); color: var(--cobalt); }}
.comment-anchor.is-active {{ outline: 2px solid var(--warning); outline-offset: 2px; }}
.comment-anchor--agent.is-active {{ outline-color: var(--cobalt); }}
.selection-tools {{ position: fixed; z-index: 20; display: inline-flex; align-items: center; gap: 10px; min-height: 38px; padding: 6px 8px 6px 12px; background: var(--ink); color: var(--paper); box-shadow: 0 12px 28px rgba(17,17,17,.2); font-size: 12px; }}
.selection-tools strong {{ color: var(--comment-wash); font-weight: 700; }}
.selection-tools button {{ min-height: 26px; padding: 0 9px; border: 1px solid rgba(251,250,247,.45); background: transparent; color: var(--paper); cursor: pointer; font-size: 11px; font-weight: 700; }}
.selection-tools button:hover {{ background: var(--paper); color: var(--ink); }}
.inline-tab {{ display: inline-block; width: 1.4em; }}
.page-break {{ display: block; height: 12px; margin: 20px 0; border-top: 1px dashed var(--hairline); }}
.reference-marker {{ color: var(--cobalt); font-size: .75em; }}
.structural-anchor {{ display: none; }}
.selection-highlight {{ position: fixed; inset: 0; z-index: 18; pointer-events: none; }}
.selection-highlight-box {{ position: fixed; border: 2px solid var(--cobalt); background: transparent; pointer-events: none; }}
.mobile-ruler {{ position: fixed; top: calc(var(--topbar-height) + 8px); right: 8px; bottom: 16px; z-index: 40; display: none; width: 44px; pointer-events: none; }}
.mobile-ruler-track {{ position: absolute; inset: 0; border: 0; background: transparent; pointer-events: auto; touch-action: none; }}
.mobile-ruler-track::before {{ content: ""; position: absolute; inset: 0 10px; background: repeating-linear-gradient(to bottom, var(--hairline) 0 2px, transparent 2px 10px); pointer-events: none; }}
.mobile-ruler-viewport {{ position: absolute; left: 50%; z-index: 3; width: 28px; height: 4px; min-height: 4px; padding: 0; border: 0; border-radius: 0; background: var(--ink); box-shadow: none; opacity: 1; transform: translate(-50%, -50%); cursor: default; pointer-events: none; touch-action: none; }}
.mobile-ruler-viewport:focus-visible {{ outline: 2px solid var(--cobalt); outline-offset: 4px; }}
.mobile-ruler-marker {{ position: absolute; left: 50%; z-index: 2; width: 44px; height: 44px; min-height: 44px; padding: 0; border: 0; border-radius: 0; background: transparent; transform: translate(-50%, -50%); cursor: pointer; pointer-events: auto; touch-action: manipulation; }}
.mobile-ruler-marker::before {{ content: ""; position: absolute; top: 50%; left: 50%; width: 24px; height: 4px; transform: translate(-50%, -50%); }}
.mobile-ruler-marker--revision::before {{ background: var(--signal); }}
.mobile-ruler-marker--comment::before {{ background: var(--warning); }}
.mobile-ruler-marker--agent::before {{ background: var(--cobalt); }}
.mobile-ruler-marker.is-active::before {{ width: 28px; height: 5px; outline: 2px solid var(--ink); outline-offset: 2px; z-index: 2; }}
.review-jump-controls {{ position: fixed; right: 28px; bottom: 24px; z-index: 50; display: flex; align-items: center; gap: 4px; padding: 4px; border: 1px solid var(--hairline); background: var(--paper); box-shadow: 0 16px 36px rgba(17,17,17,.08); }}
.review-jump-button {{ display: inline-flex; align-items: center; justify-content: center; width: 44px; height: 44px; padding: 0; border: 0; background: transparent; color: var(--ink); cursor: pointer; font-size: 18px; line-height: 1; }}
.review-jump-button:hover {{ background: var(--ink); color: var(--paper); }}
.review-jump-button:disabled {{ color: var(--ink-muted); cursor: not-allowed; opacity: .45; }}
.review-jump-status {{ min-width: 42px; color: var(--ink-muted); font: 700 10px ui-monospace, SFMono-Regular, Consolas, monospace; text-align: center; }}
.review-rail {{ position: sticky; top: calc(var(--topbar-height) + 24px); display: flex; flex-direction: column; min-width: 0; height: calc(100dvh - var(--topbar-height) - 48px); max-height: calc(100dvh - var(--topbar-height) - 48px); background: var(--paper); border: 1px solid var(--hairline); box-shadow: 0 16px 36px rgba(17,17,17,.08); }}
.rail-heading {{ padding: 20px; border-bottom: 1px solid var(--hairline); }}
.rail-heading h2 {{ margin: 0; font-size: 18px; letter-spacing: -.02em; }}
.rail-summary {{ display: flex; align-items: baseline; gap: 8px; margin-top: 12px; }}
.rail-summary strong {{ font-size: 28px; letter-spacing: -.05em; }}
.rail-summary span {{ color: var(--ink-muted); font-size: 12px; }}
.rail-tabs {{ display: grid; grid-template-columns: 1fr 1fr; border-bottom: 1px solid var(--hairline); }}
.rail-tab {{ min-height: 42px; border-right: 1px solid var(--hairline); font-size: 12px; text-align: left; padding: 0 16px; }}
.rail-tab:last-child {{ border-right: 0; }}
.rail-tab[aria-selected="true"] {{ box-shadow: inset 0 -3px 0 var(--signal); font-weight: 700; }}
.tab-count {{ color: var(--ink-muted); font: 700 11px ui-monospace, SFMono-Regular, Consolas, monospace; }}
.rail-filter {{ display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--hairline-soft); }}
.rail-filter label {{ color: var(--ink-muted); font-size: 11px; }}
.filter-buttons {{ display: inline-flex; gap: 4px; }}
.filter-button {{ padding: 4px 6px; color: var(--ink-muted); font-size: 11px; }}
.filter-button[aria-pressed="true"] {{ color: var(--ink); font-weight: 700; text-decoration: underline; text-underline-offset: 3px; }}
.review-list {{ flex: 1 1 auto; min-height: 120px; overflow: auto; }}
.review-item, .comment-item {{ display: flex; width: 100%; gap: 12px; padding: 14px 16px; border: 0; border-bottom: 1px solid var(--hairline-soft); background: transparent; cursor: pointer; text-align: left; }}
.review-item:hover, .comment-item:hover {{ background: var(--canvas); }}
.review-item.is-active, .comment-item.is-active {{ background: var(--canvas); box-shadow: inset 4px 0 0 var(--signal); }}
.review-item.is-decided .review-index {{ color: var(--success); }}
.review-index {{ flex: 0 0 24px; color: var(--signal); font: 700 11px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }}
.review-item-copy {{ min-width: 0; display: grid; gap: 4px; }}
.review-item-label {{ color: var(--signal-dark); font-size: 11px; font-weight: 700; }}
.comment-item .review-item-label {{ color: var(--warning); }}
.review-item-quote {{ overflow: hidden; color: var(--ink); font-size: 13px; line-height: 1.35; text-overflow: ellipsis; }}
.review-item-meta {{ overflow: hidden; color: var(--ink-muted); font-size: 11px; line-height: 1.3; text-overflow: ellipsis; white-space: nowrap; }}
.rail-empty {{ padding: 28px 20px; color: var(--ink-muted); font-size: 13px; line-height: 1.5; }}
.comment-section + .comment-section {{ margin-top: 8px; border-top: 8px solid var(--canvas); }}
.comment-section-title {{ display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin: 0; padding: 12px 16px 8px; color: var(--ink); font: 700 11px/1.25 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .04em; text-transform: uppercase; }}
.comment-section-title span {{ color: var(--ink-muted); font-size: 9px; font-weight: 400; letter-spacing: .08em; }}
.comment-section[data-source="word"] .comment-section-title {{ color: var(--warning); }}
.comment-section[data-source="agent"] .comment-section-title {{ color: var(--cobalt); }}
.decision-panel {{ order: 5; flex: 0 0 auto; padding: 16px; border-top: 2px solid var(--ink); background: var(--paper); }}
.comment-detail {{ display: grid; gap: 8px; padding: 16px; border-top: 2px solid var(--warning); background: var(--paper); }}
.comment-detail-title {{ margin: 0; font-size: 15px; line-height: 1.25; }}
.comment-detail-quote {{ margin: 4px 0; padding-left: 10px; border-left: 2px solid var(--warning); color: var(--ink); font-size: 13px; line-height: 1.45; }}
.comment-detail-meta {{ margin: 0; color: var(--ink-muted); font-size: 11px; line-height: 1.35; }}
.comment-detail-text {{ margin: 0; color: var(--ink); font-size: 13px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }}
.comment-detail-close {{ min-height: 34px; border: 1px solid var(--hairline); background: transparent; color: var(--ink); cursor: pointer; font-size: 12px; }}
.comment-detail-close:hover {{ background: var(--ink); color: var(--paper); }}
.decision-empty {{ color: var(--ink-muted); font-size: 12px; line-height: 1.5; }}
.decision-content[hidden], .comment-detail[hidden], .rail-list[hidden], .adjust-compose[hidden], .comment-compose[hidden] {{ display: none; }}
.decision-kicker {{ margin: 0 0 6px; color: var(--signal); font: 700 10px/1.2 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .1em; text-transform: uppercase; }}
.decision-title {{ margin: 0; font-size: 15px; line-height: 1.25; }}
.decision-quote {{ margin: 10px 0; padding-left: 10px; border-left: 2px solid var(--signal); font-size: 13px; line-height: 1.45; }}
.adjust-compose {{ display: grid; gap: 8px; }}
.adjust-compose-title {{ margin: 0; font-size: 15px; line-height: 1.25; }}
.adjust-compose-quote {{ margin: 4px 0; padding-left: 10px; border-left: 2px solid var(--signal); color: var(--ink); font-size: 13px; line-height: 1.45; }}
.adjust-compose-meta {{ margin: 0; color: var(--ink-muted); font-size: 11px; line-height: 1.35; }}
.adjust-compose-actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
.adjust-cancel {{ min-height: 34px; border: 1px solid var(--hairline); background: transparent; cursor: pointer; font-size: 12px; }}
.adjust-save {{ min-height: 34px; border: 0; background: var(--signal); color: var(--paper); cursor: pointer; font-size: 12px; font-weight: 700; }}
.adjust-save:hover {{ background: var(--signal-dark); }}
.comment-compose {{ order: 6; display: grid; gap: 8px; flex: 0 0 auto; padding: 16px; border-top: 1px solid var(--hairline); background: var(--paper); }}
.comment-compose[hidden] {{ display: none; }}
.comment-compose-title {{ margin: 0; font-size: 15px; line-height: 1.25; }}
.comment-compose-quote {{ margin: 4px 0; padding-left: 10px; border-left: 2px solid var(--cobalt); color: var(--ink); font-size: 13px; line-height: 1.45; }}
.comment-compose-meta {{ margin: 0; color: var(--ink-muted); font-size: 11px; line-height: 1.35; }}
.comment-compose-actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
.comment-cancel {{ min-height: 34px; border: 1px solid var(--hairline); background: transparent; cursor: pointer; font-size: 12px; }}
.comment-save {{ min-height: 34px; border: 0; background: var(--cobalt); color: var(--paper); cursor: pointer; font-size: 12px; font-weight: 700; }}
.comment-save:hover {{ background: var(--ink); }}
.decision-meta {{ margin: 0 0 10px; color: var(--ink-muted); font-size: 11px; }}
.decision-actions {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }}
.decision-action {{ min-height: 34px; border: 1px solid var(--hairline); background: transparent; cursor: pointer; font-size: 12px; }}
.decision-action:hover {{ border-color: var(--ink); }}
.decision-action.is-selected {{ border-color: var(--ink); background: var(--ink); color: var(--paper); }}
.decision-note {{ width: 100%; min-height: 56px; margin-top: 10px; resize: vertical; border: 1px solid var(--hairline); background: var(--paper); padding: 8px; color: var(--ink); font-size: 12px; line-height: 1.4; }}
.decision-apply {{ width: 100%; min-height: 34px; margin-top: 8px; border: 0; background: var(--cobalt); color: var(--paper); cursor: pointer; font-size: 12px; font-weight: 700; }}
.decision-apply:hover {{ background: var(--ink); }}
.decision-error {{ min-height: 16px; margin: 6px 0 0; color: var(--signal-dark); font-size: 11px; }}
.format-diagnostics {{ margin-top: 20px; border-top: 1px solid var(--hairline); color: var(--ink-muted); }}
.format-diagnostics summary {{ padding: 12px 0; cursor: pointer; font-size: 12px; list-style: none; }}
.format-diagnostics summary::-webkit-details-marker {{ display: none; }}
.format-diagnostics summary::before {{ content: "+"; display: inline-block; width: 18px; color: var(--signal); font: 700 14px ui-monospace, monospace; }}
.format-diagnostics[open] summary::before {{ content: "–"; }}
.diagnostic-status {{ float: right; color: var(--success); font: 700 10px ui-monospace, monospace; }}
.diagnostic-status.has-warning {{ color: var(--warning); }}
.audit-wrap {{ overflow-x: auto; padding-bottom: 12px; }}
.audit-table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
.audit-table th, .audit-table td {{ padding: 7px 8px; border: 1px solid var(--hairline-soft); text-align: left; vertical-align: top; }}
.audit-table th {{ color: var(--ink); font-size: 10px; text-transform: uppercase; }}
.audit-ok {{ color: var(--success); }}
.audit-warning {{ color: var(--warning); }}
.mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
[hidden] {{ display: none !important; }}
@media (max-width: 900px) {{
  .console-header {{ grid-template-columns: 1fr; gap: 20px; align-items: start; }}
  .header-actions {{ justify-content: start; }}
  .workspace {{ grid-template-columns: 1fr; gap: 24px; padding-top: 20px; }}
  .review-rail {{ order: -1; position: sticky; top: var(--topbar-height); z-index: 5; height: auto; max-height: none; }}
  .review-list {{ max-height: 240px; }}
  .decision-panel {{ position: relative; }}
}}
@media (max-width: 600px) {{
  .page-frame {{ width: min(calc(100% - 20px), 1440px); }}
  .console-header {{ padding: 14px 0 12px; gap: 10px; }}
  .console-header h1 {{ font-size: 32px; line-height: 1; }}
  .console-header p:last-child {{ display: none; }}
  .header-actions {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 8px; }}
  .header-actions .server-status {{ display: none; }}
  .view-switch {{ grid-column: 1 / -1; width: 100%; }}
  .view-button {{ min-height: 34px; flex: 1; }}
  .send-action, .primary-action {{ width: 100%; }}
  .header-rule {{ display: grid; gap: 4px; padding: 8px 0 10px; font-size: 11px; }}
  .workspace {{ gap: 16px; padding: 16px 44px 72px 0; }}
  .stage-heading {{ padding-bottom: 12px; }}
  .stage-heading h2 {{ font-size: 18px; line-height: 1.3; }}
  .stage-heading p:last-child {{ font-size: 12px; line-height: 1.4; }}
  .document-paper {{ margin-top: 10px; padding: 24px 14px 40px; }}
  .document-paragraph {{ margin-bottom: 16px; font-size: 16px; line-height: 1.72; }}
  .review-rail {{ display: contents; }}
  .review-rail > .rail-heading,
  .review-rail > .rail-tabs,
  .review-rail > .rail-filter,
  .review-rail > .review-list {{ display: none !important; }}
  .review-rail .decision-panel,
  .review-rail .comment-compose,
  .review-rail .comment-detail {{ display: none; }}
  .review-rail[data-mobile-sheet="decision"] .decision-panel {{ position: fixed; left: 10px; right: 10px; bottom: calc(8px + env(safe-area-inset-bottom)); z-index: 45; display: block; max-height: min(42dvh, 360px); overflow: auto; padding: 12px 14px calc(12px + env(safe-area-inset-bottom)); border: 1px solid var(--hairline); border-top: 2px solid var(--ink); background: var(--paper); box-shadow: 0 16px 36px rgba(17,17,17,.08); }}
  .review-rail[data-mobile-sheet="adjust"] .decision-panel {{ position: fixed; left: 10px; right: 10px; top: var(--mobile-compose-top, 120px); z-index: 45; display: block; max-height: min(42dvh, 360px); overflow: auto; padding: 12px 14px; border: 1px solid var(--hairline); border-top: 2px solid var(--signal); background: var(--paper); box-shadow: 0 16px 36px rgba(17,17,17,.08); }}
  .review-rail[data-mobile-sheet="comment"] .comment-compose {{ position: fixed; left: 10px; right: 10px; top: var(--mobile-compose-top, 120px); z-index: 45; display: grid; max-height: min(48dvh, 400px); overflow: auto; padding: 12px 14px; border: 1px solid var(--hairline); border-top: 2px solid var(--cobalt); background: var(--paper); box-shadow: 0 16px 36px rgba(17,17,17,.08); }}
  .review-rail[data-mobile-sheet="comment-detail"] .decision-panel {{ position: fixed; left: 10px; right: 10px; bottom: calc(8px + env(safe-area-inset-bottom)); z-index: 45; display: block; max-height: min(42dvh, 360px); overflow: auto; padding: 12px 14px calc(12px + env(safe-area-inset-bottom)); border: 1px solid var(--hairline); border-top: 2px solid var(--warning); background: var(--paper); box-shadow: 0 16px 36px rgba(17,17,17,.08); }}
  .review-rail[data-mobile-sheet="comment-detail"] .comment-detail {{ display: grid; padding: 0; }}
  .decision-panel, .comment-compose {{ order: initial; }}
  .decision-actions {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
  .decision-note, .comment-compose textarea {{ min-height: 44px; max-height: 88px; font-size: 16px; line-height: 1.4; -webkit-text-size-adjust: 100%; }}
  .mobile-ruler {{ right: 0; bottom: 8px; display: block; }}
  .review-jump-controls {{ right: 42px; bottom: 10px; }}
  .review-jump-button {{ width: 44px; height: 44px; }}
}}
@media (any-pointer: coarse) {{
  .rail-tab, .filter-button, .view-button, .send-action, .primary-action,
  .selection-tools button, .decision-action, .decision-apply,
  .comment-cancel, .comment-save, .comment-detail-close, .adjust-cancel, .adjust-save {{ min-height: 44px; }}
  .filter-button {{ min-width: 44px; }}
}}
@media (prefers-reduced-motion: reduce) {{
  html {{ scroll-behavior: auto; }}
  *, *::before, *::after {{ transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }}
}}
{chr(10).join(style_rules)}
"""


def _build_page(
    workdir: Path,
    document: Any,
    styles: dict[str, dict[str, Any]],
    infos: dict[str, StyleInfo],
    comments_meta: dict[str, dict[str, str]],
    revision_keys: dict[str, str],
    *,
    server_mode: bool = False,
) -> str:
    ctx = RenderContext(infos, comments_meta, revision_keys)
    paragraph_html = "".join(_paragraph_html(paragraph, ctx) for paragraph in document.paragraphs)

    # Preserve comment records without a visible anchor as a diagnostic review
    # item, but do not invent a document location.
    for comment_id, meta in comments_meta.items():
        if comment_id not in ctx._comment_seen:
            ctx.comment_records.append(
                {
                    "cid": comment_id,
                    "author": meta.get("author", ""),
                    "date": meta.get("date", ""),
                    "text": meta.get("text", ""),
                    "pid": "",
                    "order": str(len(ctx.comment_records) + 1),
                    "source": "word",
                }
            )

    revision_items = "".join(_review_item_html(record) for record in ctx.revision_records)
    comment_items = "".join(_comment_item_html(record) for record in ctx.comment_records)
    if not revision_items:
        revision_items = '<div class="rail-empty">当前文档没有可审阅修订。</div>'
    if not comment_items:
        comment_items = '<div class="rail-empty">当前文档没有批注。</div>'

    audit_rows, warning_count = _format_audit_rows(styles, infos)
    audit_status = f"{warning_count} 个样式待核对" if warning_count else "完整映射"
    source_name = str(document.meta.get("source", "document.docx"))
    source_stem = Path(source_name).stem
    if source_stem.lower() in {"source", "document", "untitled"}:
        document_title = "无标题文档"
        for paragraph in document.paragraphs:
            candidate = re.sub(r"^(发明名称|标题|题目)\s*[:：]\s*", "", _node_text(paragraph.nodes)).strip()
            if candidate:
                document_title = _clip(candidate, 42)
                break
    else:
        document_title = _clip(source_stem, 42)
    summary = f"{len(document.paragraphs)} 段 · {len(ctx.revision_records)} 处修订 · {len(ctx.comment_records)} 条批注"

    css_rules: list[str] = []
    for sid, info in infos.items():
        declarations = "; ".join(f"{key}: {value}" for key, value in info.css.items())
        css_rules.append(f".s-{_ESCAPE(sid)} {{ {declarations} }}")
    css = _css(css_rules)
    boot = {
        "source": source_name,
        "server_mode": server_mode,
        "revisions": ctx.revision_records,
        "comments": ctx.comment_records,
    }
    boot_json = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    js = r"""
const boot = __BOOT__;
const serverMode = Boolean(boot.server_mode) && /^https?:$/.test(location.protocol);
const body = document.body;
const state = {
  currentRid: null,
  currentCid: null,
  action: null,
  filter: 'all',
  tab: 'revisions',
  decisions: {},
  queue: [],
  session: null,
  currentSnapshot: 'C0',
  stagedSnapshot: 'H0',
  pendingSelection: null,
  dismissedComposer: null,
  polling: false,
};
const statusEl = document.getElementById('global-status');
const progressEl = document.getElementById('rail-progress');
const countEl = document.getElementById('decided-count');
const serverStatus = document.getElementById('server-status');
const sendButton = document.getElementById('send-agent');
const detailEmpty = document.getElementById('decision-empty');
const detailContent = document.getElementById('decision-content');
const detailKicker = document.getElementById('decision-kicker');
const detailTitle = document.getElementById('decision-title');
const detailQuote = document.getElementById('decision-quote');
const detailMeta = document.getElementById('decision-meta');
const detailNote = document.getElementById('decision-note');
const detailError = document.getElementById('decision-error');
const commentDetail = document.getElementById('comment-detail');
const commentDetailKicker = document.getElementById('comment-detail-kicker');
const commentDetailTitle = document.getElementById('comment-detail-title');
const commentDetailQuote = document.getElementById('comment-detail-quote');
const commentDetailMeta = document.getElementById('comment-detail-meta');
const commentDetailText = document.getElementById('comment-detail-text');
const commentCompose = document.getElementById('comment-compose');
const commentQuote = document.getElementById('comment-quote');
const commentMeta = document.getElementById('comment-meta');
const commentNote = document.getElementById('comment-note');
const commentError = document.getElementById('comment-error');
const adjustCompose = document.getElementById('adjust-compose');
const adjustQuote = document.getElementById('adjust-quote');
const adjustMeta = document.getElementById('adjust-meta');
const adjustText = document.getElementById('adjust-text');
const adjustError = document.getElementById('adjust-error');
const selectionTools = document.getElementById('selection-tools');
const selectionCount = document.getElementById('selection-count');
const selectionHighlight = document.getElementById('selection-highlight');
const jumpControls = document.getElementById('review-jump-controls');
const previousReviewButton = document.getElementById('previous-review');
const nextReviewButton = document.getElementById('next-review');
const reviewJumpStatus = document.getElementById('review-jump-status');
const actionButtons = [...document.querySelectorAll('.decision-action')];
const topbar = document.getElementById('topbar');
const reviewRail = document.getElementById('review-rail');
const mobileRuler = document.getElementById('mobile-ruler');
const mobileRulerTrack = document.getElementById('mobile-ruler-track');
const mobileRulerViewport = document.getElementById('mobile-ruler-viewport');
const wordCommentItems = document.getElementById('word-comment-items');
const agentCommentItems = document.getElementById('agent-comment-items');
let revisions = new Map(boot.revisions.map(item => [item.rid, item]));
let comments = new Map(boot.comments.map(item => [item.cid, item]));
let revisionMarks = [...document.querySelectorAll('.revision-mark')];
let rulerMarkerKey = '';
let rulerHapticKey = '';
let queuedCommentAnchorKey = '';
let selectionClearTimer = 0;
let selectionCaptureTimer = 0;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
}
function queuedCommentRecord(event, index) {
  return {
    cid: `event:${event.event_id}`,
    author: event.author || event.client_id || '人工审阅',
    date: event.created_at || '',
    text: event.note || event.selected_text || '',
    selected_text: event.selected_text || '',
    before_context: event.before_context || '',
    after_context: event.after_context || '',
    pid: event.paragraph_id || '',
    order: String(index + 1),
    source: 'agent',
  };
}
function commentTextNodes(root) {
  const nodes = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (node.parentElement?.closest('button, .structural-anchor')) continue;
    nodes.push(node);
  }
  return nodes;
}
function normalizeTextWithMap(value) {
  let text = '';
  const starts = [];
  const ends = [];
  let inWhitespace = false;
  for (let index = 0; index < value.length; index += 1) {
    if (/\s/.test(value[index])) {
      if (!inWhitespace) {
        text += ' ';
        starts.push(index);
        ends.push(index + 1);
        inWhitespace = true;
      } else {
        ends[ends.length - 1] = index + 1;
      }
      continue;
    }
    text += value[index];
    starts.push(index);
    ends.push(index + 1);
    inWhitespace = false;
  }
  return { text, starts, ends };
}
function rangeForCommentText(root, value) {
  const needle = String(value || '').replace(/\s+/g, ' ').trim();
  if (!needle) return null;
  const nodes = commentTextNodes(root);
  const raw = nodes.map(node => node.nodeValue || '').join('');
  if (!raw) return null;
  let start = raw.indexOf(value);
  let end = start >= 0 ? start + String(value).length : -1;
  if (start < 0) {
    const normalized = normalizeTextWithMap(raw);
    const index = normalized.text.indexOf(needle);
    if (index < 0) return null;
    start = normalized.starts[index];
    end = normalized.ends[index + needle.length - 1];
  }
  const range = document.createRange();
  const setBoundary = (method, offset) => {
    let cursor = 0;
    for (const node of nodes) {
      const length = (node.nodeValue || '').length;
      if (offset <= cursor + length) {
        range[method](node, Math.max(0, offset - cursor));
        return;
      }
      cursor += length;
    }
    const last = nodes[nodes.length - 1];
    range[method](last, (last.nodeValue || '').length);
  };
  setBoundary('setStart', start);
  setBoundary('setEnd', end);
  return range;
}
function renderQueuedCommentAnchors(records, force = false) {
  const key = records.map(record => `${record.cid}:${record.pid}:${record.selected_text}:${record.text}`).join('\u001f');
  const existing = [...document.querySelectorAll('.comment-anchor--agent')];
  if (!force && key === queuedCommentAnchorKey && existing.length === records.length) return;
  existing.forEach(marker => marker.remove());
  records.forEach(record => {
    const paragraph = [...document.querySelectorAll('.document-paragraph')].find(item => item.dataset.pid === record.pid);
    const range = paragraph && rangeForCommentText(paragraph, record.selected_text);
    if (!range) return;
    range.collapse(false);
    const marker = document.createElement('button');
    marker.type = 'button';
    marker.className = 'comment-anchor comment-anchor--agent';
    marker.dataset.cid = record.cid;
    marker.textContent = `A${record.order}`;
    marker.title = `审阅批注：${record.text || '空批注'}`;
    marker.setAttribute('aria-label', `审阅批注 ${record.order}：${record.text || '空批注'}`);
    range.insertNode(marker);
  });
  queuedCommentAnchorKey = key;
  rulerMarkerKey = '';
  bindReviewTargets();
}
function renderQueuedComments(forceAnchors = false) {
  if (!agentCommentItems) return;
  for (const cid of comments.keys()) if (cid.startsWith('event:')) comments.delete(cid);
  const records = state.queue
    .filter(event => event.type === 'comment' && event.event_id)
    .sort((left, right) => String(left.created_at || '').localeCompare(String(right.created_at || '')))
    .map(queuedCommentRecord);
  records.forEach(record => comments.set(record.cid, record));
  renderQueuedCommentAnchors(records, forceAnchors);
  agentCommentItems.innerHTML = records.length
    ? records.map(record => `
      <button class="comment-item" data-cid="${escapeHtml(record.cid)}">
        <span class="review-index">${escapeHtml(record.order.padStart(2, '0'))}</span>
        <span class="review-item-copy">
          <span class="review-item-label">新增批注</span>
          <span class="review-item-quote">${escapeHtml(record.text || '空批注')}</span>
          <span class="review-item-meta">${escapeHtml(itemMeta(record))}${record.pid ? ` · 段落 ${escapeHtml(record.pid.replace(/^P/, ''))}` : ''}</span>
        </span>
      </button>`).join('')
    : '<p class="rail-empty">尚无新增批注</p>';
  const counts = [...document.querySelectorAll('.tab-count')];
  if (counts[1]) counts[1].textContent = String(comments.size);
  bindReviewTargets();
}
function itemMeta(item) {
  return [item.author, item.date ? item.date.slice(0, 10).replaceAll('-', '.') : ''].filter(Boolean).join(' · ') || '未标注作者';
}
function decisionCount() { return Object.keys(state.decisions).length; }
function selectedDecision(rid) { return state.decisions[rid] || null; }
function revisionSequence() {
  return [...revisions.values()].sort((left, right) => Number(left.order) - Number(right.order));
}
function updateReviewJumpControls() {
  if (!jumpControls) return;
  const sequence = revisionSequence();
  jumpControls.hidden = sequence.length === 0;
  const index = state.currentRid ? sequence.findIndex(item => item.rid === state.currentRid) : -1;
  setText(reviewJumpStatus, `${index >= 0 ? index + 1 : 0} / ${sequence.length}`);
  previousReviewButton.disabled = index <= 0;
  nextReviewButton.disabled = index >= sequence.length - 1;
}
function jumpRevision(offset) {
  const sequence = revisionSequence();
  if (!sequence.length) return;
  const index = state.currentRid ? sequence.findIndex(item => item.rid === state.currentRid) : -1;
  const targetIndex = Math.min(sequence.length - 1, Math.max(0, (index >= 0 ? index : offset > 0 ? -1 : 0) + offset));
  const target = sequence[targetIndex];
  setTab('revisions');
  state.filter = 'all';
  applyFilter();
  setCurrentRevision(target.rid, true);
}
function setText(el, text) { if (el) el.textContent = text || ''; }
function queueStorageKey() { return `docx2typed-review:${boot.source}`; }
function newEventId() {
  return `local-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}
function readLocalQueue() {
  try { return JSON.parse(localStorage.getItem(queueStorageKey()) || '[]'); } catch (_) { return []; }
}
function writeLocalQueue() {
  try { localStorage.setItem(queueStorageKey(), JSON.stringify(state.queue)); } catch (_) {}
}
function mergeQueueEvent(event) {
  if (!event) return;
  const index = state.queue.findIndex(item => item.event_id === event.event_id);
  if (index < 0) {
    state.queue.push(event);
  } else if (state.queue[index].status === 'draft' && ['draft', 'queued'].includes(event.status)) {
    state.queue[index] = event;
  }
  hydrateDecisions();
  renderQueuedComments();
  updateQueueStatus();
}

function applySession(session) {
  if (!session) return;
  state.session = session;
  state.currentSnapshot = session.current_snapshot?.id || state.currentSnapshot;
  state.stagedSnapshot = session.staged_snapshot?.id || state.currentSnapshot;
}
function patchParentSnapshot() {
  const staged = state.session?.staged_snapshot;
  return staged?.patch_ids?.length ? state.stagedSnapshot : state.currentSnapshot;
}

function hydrateDecisions() {
  state.decisions = {};
  state.queue.filter(event => event.type === 'decision').forEach(event => {
    const rid = event.revision_id || [...revisions.values()].find(item => item.key === event.revision_key)?.rid;
    if (rid) state.decisions[rid] = { revision_key: event.revision_key, decision: event.decision, comment: event.comment || null };
  });
}

function updateQueueStatus(message) {
  const drafts = state.queue.filter(event => event.status === 'draft').length;
  const queued = state.queue.filter(event => event.status === 'queued' && !['applied', 'acknowledged'].includes(event.delivery_state)).length;
  const snapshotLabel = state.currentSnapshot ? ` · ${state.currentSnapshot}` : '';
  if (message) setText(serverStatus, message);
  else setText(serverStatus, serverMode ? `LOCAL SERVER${snapshotLabel} · 草稿 ${drafts} · 待 agent ${queued}` : `离线预览 · 待发送 ${drafts}`);
  setText(sendButton, serverMode ? `发送给 agent${drafts ? ` (${drafts})` : ''}` : `导出给 agent${drafts ? ` (${drafts})` : ''}`);
  sendButton.disabled = drafts === 0;
}

async function loadQueue() {
  try {
    if (serverMode) {
      const response = await fetch('/api/reviews', { cache: 'no-store' });
      if (!response.ok) throw new Error('server unavailable');
      const data = await response.json();
      state.queue = data.events || [];
      applySession(data.session);
    } else {
      state.queue = readLocalQueue();
    }
    hydrateDecisions();
    renderQueuedComments();
    updateStats();
    updateQueueStatus();
  } catch (error) {
    updateQueueStatus('SERVER ERROR · 请检查 review server');
  }
}
async function persistEvent(event) {
  const payload = event.type === 'patch'
    ? { ...event, parent_snapshot: event.parent_snapshot || patchParentSnapshot() || 'C0' }
    : event;
  if (serverMode) {
    const response = await fetch('/api/reviews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    applySession(data.session);
    return data.event;
  }
  const now = new Date().toISOString();
  const record = {
    ...payload,
    event_id: payload.event_id || newEventId(),
    review_item_id: payload.review_item_id || `${payload.type}:${payload.client_id || newEventId()}`,
    delivery_state: 'staged',
    status: 'draft',
    created_at: now,
    updated_at: now,
  };
  mergeQueueEvent(record);
  writeLocalQueue();
  return record;
}
async function dispatchToAgent() {
  const drafts = state.queue.filter(event => event.status === 'draft');
  if (!drafts.length) return;
  try {
    if (serverMode) {
      const response = await fetch('/api/reviews/dispatch', { method: 'POST' });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      applySession(data.session);
      data.events.forEach(mergeQueueEvent);
      updateQueueStatus(`已发送 ${data.events.length} 条 · 等待 agent 读取`);
    } else {
      const batchId = `batch-${newEventId()}`;
      const events = state.queue.map(event => event.status === 'draft'
        ? { ...event, status: 'queued', delivery_state: 'queued', batch_id: batchId, queued_at: new Date().toISOString() }
        : event);
      state.queue = events;
      writeLocalQueue();
      const blob = new Blob([JSON.stringify({ schema: 'docx2typed-review-inbox-1', source: boot.source, events }, null, 2)], { type: 'application/json' });
      const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'review-inbox.json'; link.click();
      updateQueueStatus(`已导出 ${drafts.length} 条 · 可交给 agent`);
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    }
  } catch (error) {
    updateQueueStatus('发送失败 · review server 不可用');
  }
}
function updateStats() {
  const total = revisions.size;
  const decided = decisionCount();
  setText(countEl, String(decided).padStart(2, '0'));
  setText(progressEl, `${decided} / ${total} 已决策`);
  setText(statusEl, total ? `${total} 处修订 · ${decided} 已决策 · ${total - decided} 待审` : '文档无待审修订');
  document.querySelectorAll('.review-item').forEach(item => {
    const decision = selectedDecision(item.dataset.rid);
    item.dataset.status = decision ? 'decided' : 'pending';
    item.classList.toggle('is-decided', Boolean(decision));
  });
  applyFilter();
  updateReviewJumpControls();
  updateQueueStatus();
}
function activeRevisionElement(rid) { return revisionMarks.find(el => el.dataset.rid === rid) || null; }
function isMobileViewport() {
  return window.matchMedia('(max-width: 600px)').matches;
}
function setMobileSheet(sheet) {
  if (!reviewRail || !isMobileViewport()) return;
  if (sheet) reviewRail.dataset.mobileSheet = sheet;
  else delete reviewRail.dataset.mobileSheet;
}
function clearSelectionHighlight() {
  if (!selectionHighlight) return;
  selectionHighlight.hidden = true;
  selectionHighlight.replaceChildren();
}
function renderSelectionHighlight() {
  const range = state.pendingSelection?.range;
  if (!selectionHighlight || !range) return clearSelectionHighlight();
  const rects = [...range.getClientRects()];
  const visibleRects = rects.filter(rect => rect.width > 0 && rect.height > 0);
  const lastRect = visibleRects[visibleRects.length - 1];
  if (lastRect && state.pendingSelection) {
    state.pendingSelection.anchor_rect = { left: lastRect.left, top: lastRect.top, right: lastRect.right, bottom: lastRect.bottom };
  }
  const boxes = visibleRects.map(rect => {
    const box = document.createElement('span');
    box.className = 'selection-highlight-box';
    box.style.left = `${rect.left - 2}px`;
    box.style.top = `${rect.top - 2}px`;
    box.style.width = `${rect.width + 4}px`;
    box.style.height = `${rect.height + 4}px`;
    return box;
  });
  selectionHighlight.replaceChildren(...boxes);
  selectionHighlight.hidden = boxes.length === 0;
}
function positionMobileComposer(element) {
  if (!isMobileViewport() || !element || !state.pendingSelection?.anchor_rect) return;
  const target = element.id === 'adjust-compose' ? element.closest('.decision-panel') : element;
  if (!target) return;
  requestAnimationFrame(() => {
    if (element.hidden || target.hidden) return;
    const anchor = state.pendingSelection.anchor_rect;
    const maxTop = Math.max(8, window.innerHeight - target.offsetHeight - 12);
    const top = Math.max(8, Math.min(maxTop, anchor.bottom + 8));
    target.style.setProperty('--mobile-compose-top', `${top}px`);
  });
}
function pagePositionFor(element, paragraphId) {
  const target = element || [...document.querySelectorAll('.document-paragraph')].find(item => item.dataset.pid === paragraphId);
  if (!target) return null;
  const rect = target.getBoundingClientRect();
  return rect.top + window.scrollY + rect.height / 2;
}
function rulerScrollRange() {
  return Math.max(1, Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 1) - window.innerHeight);
}
function clampRulerProgress(progress) {
  return Math.min(1, Math.max(0, Number.isFinite(progress) ? progress : 0));
}
function rulerProgressForScroll() {
  return clampRulerProgress(window.scrollY / rulerScrollRange());
}
function rulerProgressFromClientY(clientY) {
  if (!mobileRulerTrack) return 0;
  const track = mobileRulerTrack.getBoundingClientRect();
  return clampRulerProgress((clientY - track.top) / Math.max(1, track.height));
}
function nearestRulerEntry(progress) {
  if (!rulerEntries.length || !mobileRulerTrack) return null;
  const trackHeight = Math.max(1, mobileRulerTrack.getBoundingClientRect().height);
  const threshold = Math.max(24 / trackHeight, 0.025);
  let nearest = null;
  let distance = Infinity;
  rulerEntries.forEach(entry => {
    const nextDistance = Math.abs(entry.progress - progress);
    if (nextDistance < distance) {
      distance = nextDistance;
      nearest = entry;
    }
  });
  return distance <= threshold ? nearest : null;
}
function updateRulerValue(progress, entry = nearestRulerEntry(progress)) {
  if (!mobileRulerViewport) return;
  const value = Math.round(clampRulerProgress(progress) * 100);
  mobileRulerViewport.setAttribute('aria-valuenow', String(value));
  mobileRulerViewport.setAttribute('aria-valuetext', entry ? entry.label : `文档位置 ${value}%`);
}
function triggerRulerHaptic(entry) {
  const key = entry ? `${entry.type}:${entry.id}` : '';
  if (entry && key !== rulerHapticKey) {
    navigator.vibrate?.(8);
    rulerHapticKey = key;
  }
  if (!entry) rulerHapticKey = '';
}
function setRulerScroll(progress, { snap = false, behavior = 'auto', haptic = false } = {}) {
  const rawProgress = clampRulerProgress(progress);
  const entry = snap ? nearestRulerEntry(rawProgress) : null;
  const targetProgress = entry ? entry.progress : rawProgress;
  const range = rulerScrollRange();
  const targetTop = entry
    ? Math.min(range, Math.max(0, entry.pageY - window.innerHeight / 2))
    : targetProgress * range;
  window.scrollTo({ top: targetTop, behavior });
  updateRulerValue(targetProgress, entry);
  if (entry) setActiveRulerMarker(entry.type, entry.id); else setActiveRulerMarker('', '');
  if (haptic) triggerRulerHaptic(entry);
  return entry;
}
function renderMobileRuler() {
  if (!mobileRulerTrack || !mobileRulerViewport) return;
  const trackHeight = Math.max(1, mobileRulerTrack.getBoundingClientRect().height);
  const documentHeight = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 1);
  const scrollRange = Math.max(1, documentHeight - window.innerHeight);
  const entries = [];
  revisions.forEach(item => {
    const pageY = pagePositionFor(activeRevisionElement(item.rid), item.pid);
    if (pageY !== null) entries.push({ id: item.rid, type: 'revision', markerType: 'revision', pageY, label: `修订：${item.text || '无文本修订'}` });
  });
  comments.forEach(item => {
    const anchor = [...document.querySelectorAll('.comment-anchor')].find(element => element.dataset.cid === item.cid);
    const pageY = pagePositionFor(anchor, item.pid);
    if (pageY !== null) entries.push({
      id: item.cid,
      type: 'comment',
      markerType: item.source === 'agent' ? 'agent' : 'comment',
      pageY,
      label: `${item.source === 'agent' ? '审阅批注' : '批注'}：${item.text || '无文本批注'}`,
    });
  });
  rulerEntries = entries.map(entry => ({
    ...entry,
    progress: clampRulerProgress(Math.max(0, Math.min(scrollRange, entry.pageY - window.innerHeight / 2)) / scrollRange),
  }));
  const markerKey = rulerEntries.map(entry => `${entry.markerType}:${entry.id}:${entry.label}`).join('\u001f');
  if (markerKey !== rulerMarkerKey) {
    mobileRulerTrack.replaceChildren(mobileRulerViewport);
    rulerEntries.forEach(entry => {
      const marker = document.createElement('button');
      marker.type = 'button';
      marker.className = `mobile-ruler-marker mobile-ruler-marker--${entry.markerType}`;
      marker.dataset.id = entry.id;
      marker.dataset.type = entry.type;
      marker.title = entry.label;
      marker.setAttribute('aria-label', entry.label);
      marker.addEventListener('click', () => {
        if (rulerWasDragged) {
          rulerWasDragged = false;
          return;
        }
        if (entry.type === 'revision') {
          setTab('revisions');
          state.filter = 'all';
          applyFilter();
          setCurrentRevision(entry.id, true, 'auto');
        } else {
          setTab('comments');
          setCurrentComment(entry.id, true, 'auto');
        }
      });
      mobileRulerTrack.append(marker);
    });
    rulerMarkerKey = markerKey;
  }
  const progress = clampRulerProgress(window.scrollY / scrollRange);
  mobileRulerViewport.style.height = '4px';
  mobileRulerViewport.style.minHeight = '4px';
  mobileRulerViewport.style.top = `${progress * trackHeight}px`;
  const markerNodes = new Map([...mobileRulerTrack.querySelectorAll('.mobile-ruler-marker')].map(marker => [marker.dataset.id, marker]));
  rulerEntries.forEach(entry => {
    const marker = markerNodes.get(entry.id);
    if (marker) {
      marker.style.top = `${entry.progress * trackHeight}px`;
      marker.dataset.progress = String(entry.progress);
    }
  });
  updateRulerValue(progress);
  if (activeRulerMarker) setActiveRulerMarker(activeRulerMarker.type, activeRulerMarker.id);
}
let activeRulerMarker = null;
function setActiveRulerMarker(type, id) {
  activeRulerMarker = type && id ? { type, id } : null;
  document.querySelectorAll('.mobile-ruler-marker').forEach(marker => {
    marker.classList.toggle('is-active', marker.dataset.type === type && marker.dataset.id === id);
  });
}
let rulerEntries = [];
let rulerDrag = null;
let rulerWasDragged = false;
function beginRulerDrag(event) {
  if (!mobileRulerTrack || (event.pointerType === 'mouse' && event.button !== 0)) return;
  const onMarker = event.target instanceof Element && Boolean(event.target.closest('.mobile-ruler-marker'));
  if (!onMarker) event.preventDefault();
  rulerWasDragged = false;
  rulerDrag = { pointerId: event.pointerId, startY: event.clientY };
  try { mobileRulerTrack.setPointerCapture(event.pointerId); } catch (_) {}
  setRulerScroll(rulerProgressFromClientY(event.clientY), { snap: true, haptic: true });
}
function moveRulerDrag(event) {
  if (!rulerDrag || event.pointerId !== rulerDrag.pointerId) return;
  if (Math.abs(event.clientY - rulerDrag.startY) > 2) rulerWasDragged = true;
  event.preventDefault();
  setRulerScroll(rulerProgressFromClientY(event.clientY), { snap: true, haptic: true });
}
function endRulerDrag(event) {
  if (!rulerDrag || event.pointerId !== rulerDrag.pointerId) return;
  const progress = rulerProgressFromClientY(event.clientY);
  setRulerScroll(progress, { snap: true, haptic: true });
  try { mobileRulerTrack.releasePointerCapture(event.pointerId); } catch (_) {}
  rulerDrag = null;
}
function handleRulerKeydown(event) {
  const current = rulerProgressForScroll();
  const step = event.key === 'PageUp' || event.key === 'PageDown' ? 0.1 : 0.02;
  let next = null;
  if (event.key === 'ArrowUp' || event.key === 'ArrowLeft' || event.key === 'PageUp') next = current - step;
  if (event.key === 'ArrowDown' || event.key === 'ArrowRight' || event.key === 'PageDown') next = current + step;
  if (event.key === 'Home') next = 0;
  if (event.key === 'End') next = 1;
  if (next === null) return;
  event.preventDefault();
  setRulerScroll(next, { snap: true });
}
let rulerFrame = 0;
function scheduleMobileRuler() {
  if (rulerFrame) return;
  rulerFrame = requestAnimationFrame(() => {
    rulerFrame = 0;
    renderMobileRuler();
    if (!commentCompose.hidden || !adjustCompose.hidden) renderSelectionHighlight();
    positionMobileComposer(commentCompose);
    positionMobileComposer(adjustCompose);
  });
}
function setCurrentRevision(rid, shouldScroll, scrollBehavior = 'smooth') {
  const item = revisions.get(rid);
  if (!item) return;
  state.currentRid = rid;
  state.currentCid = null;
  state.action = selectedDecision(rid)?.decision || null;
  commentCompose.hidden = true;
  adjustCompose.hidden = true;
  commentDetail.hidden = true;
  document.querySelectorAll('.revision-mark').forEach(el => {
    el.classList.toggle('is-active', el.dataset.rid === rid);
    el.dataset.decision = selectedDecision(rid)?.decision || '';
  });
  document.querySelectorAll('.review-item').forEach(el => {
    const active = el.dataset.rid === rid;
    el.classList.toggle('is-active', active);
    if (active) el.setAttribute('aria-current', 'true'); else el.removeAttribute('aria-current');
  });
  detailEmpty.hidden = true;
  detailContent.hidden = false;
  setText(detailKicker, `${item.order.padStart(2, '0')} / ${item.kind === 'delete' ? '删除' : item.kind === 'insert' ? '插入' : '移动'}`);
  setText(detailTitle, item.text || '无文本修订');
  setText(detailQuote, `“${item.text || '无文本修订'}”`);
  setText(detailMeta, `${itemMeta(item)} · 段落位置 ${item.pid.replace(/^P/, '') || '未知'}`);
  detailNote.value = selectedDecision(rid)?.comment || '';
  detailError.textContent = '';
  actionButtons.forEach(button => button.classList.toggle('is-selected', button.dataset.action === state.action));
  updateReviewJumpControls();
  setActiveRulerMarker('revision', rid);
  if (isMobileViewport()) setMobileSheet('decision');
  if (shouldScroll) {
    const element = activeRevisionElement(rid);
    if (element) element.scrollIntoView({ behavior: scrollBehavior === 'auto' || window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' });
  }
}
function setCurrentComment(cid, shouldScroll, scrollBehavior = 'smooth') {
  const item = comments.get(cid);
  if (!item) return;
  state.currentCid = cid;
  state.currentRid = null;
  commentCompose.hidden = true;
  adjustCompose.hidden = true;
  detailContent.hidden = true;
  detailEmpty.hidden = true;
  commentDetail.hidden = false;
  setText(commentDetailKicker, item.source === 'agent' ? 'AGENT NOTE / REVIEW COMMENT' : 'WORD COMMENT / SOURCE NOTE');
  setText(commentDetailTitle, item.source === 'agent' ? '新增审阅批注' : '原文批注');
  setText(commentDetailQuote, item.source === 'agent' ? '这条意见会作为 agent 的下一轮输入。' : '这条批注来自原始 Word 文档，内容保持不变。');
  setText(commentDetailMeta, `${itemMeta(item)} · 段落位置 ${String(item.pid || '').replace(/^P/, '') || '未知'}`);
  setText(commentDetailText, item.text || '（空批注）');
  document.querySelectorAll('.comment-item').forEach(el => el.classList.toggle('is-active', el.dataset.cid === cid));
  document.querySelectorAll('.comment-anchor').forEach(el => el.classList.toggle('is-active', el.dataset.cid === cid));
  setActiveRulerMarker('comment', cid);
  updateReviewJumpControls();
  if (isMobileViewport()) setMobileSheet('comment-detail');
  if (shouldScroll) {
    const element = [...document.querySelectorAll('.comment-anchor')].find(el => el.dataset.cid === cid)
      || [...document.querySelectorAll('.document-paragraph')].find(el => el.dataset.pid === item.pid);
    if (element) element.scrollIntoView({ behavior: scrollBehavior === 'auto' || window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' });
  }
}
function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.rail-tab').forEach(button => button.setAttribute('aria-selected', String(button.dataset.tab === tab)));
  document.getElementById('revision-list').hidden = tab !== 'revisions';
  document.getElementById('comment-list').hidden = tab !== 'comments';
  document.getElementById('revision-filter').hidden = tab !== 'revisions';
}
function applyFilter() {
  document.querySelectorAll('.review-item').forEach(item => {
    item.hidden = state.filter !== 'all' && item.dataset.status !== state.filter;
  });
  document.querySelectorAll('.filter-button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.filter === state.filter)));
}
function setView(view) {
  body.dataset.view = view;
  document.querySelectorAll('.view-button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === view)));
}
async function applyDecision() {
  if (!state.currentRid) return;
  const item = revisions.get(state.currentRid);
  const note = detailNote.value.trim();
  const decision = state.action || (note ? 'comment' : null);
  if (!decision) { detailError.textContent = '请选择接受、拒绝或暂缓，或先留下意见。'; return; }
  state.decisions[state.currentRid] = { revision_key: item.key, decision, comment: note || null };
  updateStats();
  try {
    const saved = await persistEvent({
      type: 'decision',
      client_id: `decision:${item.rid}`,
      revision_id: item.rid,
      revision_key: item.key,
      paragraph_id: item.pid,
      selected_text: item.text,
      decision,
      comment: note || '',
    });
    mergeQueueEvent(saved);
    detailError.textContent = serverMode ? '已暂存到 server · 点击“发送给 agent”后回传' : '已保存为本地草稿';
  } catch (error) {
    detailError.textContent = '保存失败 · 请检查 server 状态';
  }
}
function exportDecisions() {
  const payload = { schema: 'docx2typed-review-decisions-1', source: boot.source, decisions: Object.values(state.decisions) };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'review-decisions.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
function selectionParent(node) {
  return node && (node.nodeType === 1 ? node : node.parentElement);
}
function fingerprint(text) {
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `fnv1a-${(hash >>> 0).toString(16).padStart(8, '0')}`;
}
function paragraphPlainText(paragraph) {
  const clone = paragraph.cloneNode(true);
  clone.querySelectorAll('.comment-anchor, .structural-anchor').forEach(node => node.remove());
  return (clone.innerText || clone.textContent || '').replace(/\s+/g, ' ').trim();
}
function clearSelectionSurface() {
  state.pendingSelection = null;
  selectionTools.hidden = true;
  if (commentCompose.hidden && adjustCompose.hidden) clearSelectionHighlight();
}
function deferSelectionSurfaceClear(force = false) {
  window.clearTimeout(selectionClearTimer);
  selectionClearTimer = window.setTimeout(() => {
    const current = window.getSelection();
    if (force || !current || current.isCollapsed || !current.toString().trim()) clearSelectionSurface();
  }, 120);
}
function captureSelection() {
  const selection = window.getSelection();
  const paper = document.querySelector('.document-paper');
  if (!selection || selection.isCollapsed || !paper || !selection.toString().trim()) { deferSelectionSurfaceClear(); return; }
  window.clearTimeout(selectionClearTimer);
  const anchor = selectionParent(selection.anchorNode);
  const focus = selectionParent(selection.focusNode);
  const paragraph = anchor?.closest('.document-paragraph');
  if (!paragraph || paragraph !== focus?.closest('.document-paragraph') || !paper.contains(anchor)) { deferSelectionSurfaceClear(true); return; }
  const text = selection.toString().replace(/\s+/g, ' ').trim();
  const paragraphText = paragraphPlainText(paragraph);
  const offset = paragraphText.indexOf(text);
  if (offset < 0) { deferSelectionSurfaceClear(true); return; }
  const style_region_ids = [...new Set([...paragraph.querySelectorAll('[data-s]')].map(node => node.dataset.s).filter(Boolean))];
  const range = selection.getRangeAt(0);
  const rects = [...range.getClientRects()];
  const lastRect = rects[rects.length - 1] || range.getBoundingClientRect();
  state.pendingSelection = {
    range: range.cloneRange(),
    anchor_rect: { left: lastRect.left, top: lastRect.top, right: lastRect.right, bottom: lastRect.bottom },
    text,
    paragraph_id: paragraph.dataset.pid || '',
    before_context: offset >= 0 ? paragraphText.slice(Math.max(0, offset - 100), offset) : '',
    after_context: offset >= 0 ? paragraphText.slice(offset + text.length, offset + text.length + 100) : '',
    target: {
      start_offset: Math.max(0, offset),
      end_offset: Math.max(0, offset) + text.length,
      expected_text: text,
      left_context: offset >= 0 ? paragraphText.slice(Math.max(0, offset - 100), offset) : '',
      right_context: offset >= 0 ? paragraphText.slice(offset + text.length, offset + text.length + 100) : '',
      paragraph_fingerprint: fingerprint(paragraphText),
      region_fingerprint: fingerprint(text),
      style_region_ids,
    },
  };
  setText(selectionCount, `${text.length} 字`);
  const rect = range.getBoundingClientRect();
  selectionTools.style.left = `${Math.max(12, Math.min(window.innerWidth - selectionTools.offsetWidth - 12, rect.left + rect.width / 2 - 80))}px`;
  const toolbarTop = Math.max(12, Math.min(window.innerHeight - selectionTools.offsetHeight - 12, rect.top - 48));
  selectionTools.style.top = `${toolbarTop}px`;
  selectionTools.hidden = false;
}
function samePendingSelection(left, right) {
  return Boolean(left && right && left.paragraph_id === right.paragraph_id
    && left.target?.start_offset === right.target?.start_offset
    && left.target?.end_offset === right.target?.end_offset);
}

function openAdjustmentComposer() {
  if (!state.pendingSelection) return;
  const item = state.pendingSelection;
  const dismissed = state.dismissedComposer?.type === 'adjust' && samePendingSelection(state.dismissedComposer.selection, item) ? state.dismissedComposer.value : item.text;
  commentDetail.hidden = true;
  state.dismissedComposer = null;
  setTab('revisions');
  if (isMobileViewport()) setMobileSheet('adjust');
  detailEmpty.hidden = true;
  detailContent.hidden = true;
  commentCompose.hidden = true;
  adjustCompose.hidden = false;
  setText(adjustQuote, `“${item.text}”`);
  setText(adjustMeta, `段落位置 ${item.paragraph_id.replace(/^P/, '') || '未知'} · 生成一条带前置条件的文本 patch`);
  adjustText.value = dismissed;
  adjustError.textContent = '';
  selectionTools.hidden = true;
  renderSelectionHighlight();
  positionMobileComposer(adjustCompose);
  adjustText.focus();
}

async function saveAdjustment() {
  const item = state.pendingSelection;
  const after = adjustText.value;
  if (!item) return;
  if (!item.target || !item.paragraph_id) { adjustError.textContent = '无法确定正文锚点，请重新选择文本。'; return; }
  if (after === item.text) { adjustError.textContent = '调整后的文本不能与原文相同。'; return; }
  try {
    const saved = await persistEvent({
      type: 'patch',
      client_id: `patch:${newEventId()}`,
      review_item_id: `patch:${item.paragraph_id}:${newEventId()}`,
      origin: 'human_ui',
      author: 'human_ui',
      paragraph_id: item.paragraph_id,
      kind: 'replace',
      target: item.target,
      before: item.text,
      after,
    });
    mergeQueueEvent(saved);
    adjustError.textContent = serverMode ? 'patch 已暂存 · 点击“发送给 agent”后回传' : 'patch 已保存为本地草稿';
    state.pendingSelection = null;
    state.dismissedComposer = null;
    clearSelectionHighlight();
    adjustText.value = '';
  } catch (error) {
    adjustError.textContent = '保存失败 · 可能已有新版本，请重新读取正文。';
  }
}

function cancelAdjustment() {
  state.pendingSelection = null;
  state.dismissedComposer = null;
  clearSelectionHighlight();
  adjustCompose.hidden = true;
  if (isMobileViewport()) setMobileSheet(null);
  if (!state.currentRid) detailEmpty.hidden = false;
}

function openCommentComposer() {
  if (!state.pendingSelection) return;
  const item = state.pendingSelection;
  const dismissed = state.dismissedComposer?.type === 'comment' && samePendingSelection(state.dismissedComposer.selection, item) ? state.dismissedComposer.value : '';
  state.dismissedComposer = null;
  commentDetail.hidden = true;
  setTab('comments');
  if (isMobileViewport()) setMobileSheet('comment');
  detailEmpty.hidden = true;
  detailContent.hidden = true;
  commentCompose.hidden = false;
  adjustCompose.hidden = true;
  setText(commentQuote, `“${item.text}”`);
  setText(commentMeta, `段落位置 ${item.paragraph_id.replace(/^P/, '') || '未知'} · 这是一条发给 agent 的新批注`);
  commentNote.value = dismissed;
  commentError.textContent = '';
  selectionTools.hidden = true;
  renderSelectionHighlight();
  positionMobileComposer(commentCompose);
  commentNote.focus({ preventScroll: true });
}
async function saveComment() {
  const item = state.pendingSelection;
  const note = commentNote.value.trim();
  if (!item) return;
  if (!note) { commentError.textContent = '请先写下批注内容。'; return; }
  try {
    const saved = await persistEvent({
      type: 'comment',
      client_id: `comment:${newEventId()}`,
      paragraph_id: item.paragraph_id,
      selected_text: item.text,
      before_context: item.before_context,
      after_context: item.after_context,
      note,
    });
    commentError.textContent = serverMode ? '已暂存 · 点击“发送给 agent”后回传' : '已保存为本地草稿';
    state.pendingSelection = null;
    state.dismissedComposer = null;
    clearSelectionHighlight();
    commentNote.value = '';
  } catch (error) {
    commentError.textContent = '保存失败 · 请检查 server 状态';
  }
}
function cancelComment() {
  state.pendingSelection = null;
  state.dismissedComposer = null;
  clearSelectionHighlight();
  commentCompose.hidden = true;
  if (isMobileViewport()) setMobileSheet(null);
  if (!state.currentRid) detailEmpty.hidden = false;
  commentDetail.hidden = true;
}
function clearReviewSelection() {
  state.currentRid = null;
  state.currentCid = null;
  state.action = null;
  commentDetail.hidden = true;
  detailContent.hidden = true;
  detailEmpty.hidden = false;
  setText(detailEmpty, '从右侧索引选择一条修订，正文会自动定位到对应句子。');
  document.querySelectorAll('.revision-mark, .review-item, .comment-item, .comment-anchor').forEach(element => {
    element.classList.remove('is-active');
    element.removeAttribute('aria-current');
  });
  setActiveRulerMarker('', '');
  updateReviewJumpControls();
}
function dismissReviewSurface() {
  const editor = !commentCompose.hidden
    ? { type: 'comment', selection: state.pendingSelection, value: commentNote.value }
    : !adjustCompose.hidden
      ? { type: 'adjust', selection: state.pendingSelection, value: adjustText.value }
      : null;
  state.dismissedComposer = editor?.selection ? editor : null;
  if (!editor) state.pendingSelection = null;
  commentCompose.hidden = true;
  adjustCompose.hidden = true;
  selectionTools.hidden = true;
  clearSelectionHighlight();
  clearReviewSelection();
  if (isMobileViewport()) setMobileSheet(null);
}
function isReviewSurfaceTarget(target) {
  return target instanceof Element && Boolean(target.closest('#review-rail, #selection-tools, #mobile-ruler, #review-jump-controls'));
}

function applyDocumentFragment(data) {
  const paper = document.querySelector('.document-paper');
  if (!paper || !data?.html) return;
  const activeRid = state.currentRid;
  const activeCid = state.currentCid;
  paper.innerHTML = data.html;
  const revisionList = document.getElementById('revision-list');
  const wordItems = document.getElementById('word-comment-items');
  if (revisionList) revisionList.innerHTML = data.revision_items || '';
  if (wordItems) wordItems.innerHTML = data.comment_items || '';
  revisions = new Map((data.revisions || []).map(item => [item.rid, item]));
  comments = new Map((data.comments || []).map(item => [item.cid, item]));
  renderQueuedComments(true);
  Object.keys(state.decisions).forEach(rid => { if (!revisions.has(rid)) delete state.decisions[rid]; });
  const counts = [...document.querySelectorAll('.tab-count')];
  if (counts[0]) counts[0].textContent = String(revisions.size);
  if (counts[1]) counts[1].textContent = String(comments.size);
  bindReviewTargets();
  updateStats();
  if (activeRid && revisions.has(activeRid)) setCurrentRevision(activeRid, false);
  else if (activeCid && comments.has(activeCid)) setCurrentComment(activeCid, false);
}

async function pollDocument() {
  if (!serverMode || state.polling) return;
  state.polling = true;
  try {
    const response = await fetch('/api/document-fragment', { cache: 'no-store' });
    if (!response.ok) throw new Error('server unavailable');
    const data = await response.json();
    const nextSnapshot = data.session?.current_snapshot?.id;
    const changed = nextSnapshot && nextSnapshot !== state.currentSnapshot;
    if (data.review?.events) {
      state.queue = data.review.events;
      hydrateDecisions();
      renderQueuedComments();
      updateStats();
    }
    if (changed) {
      applyDocumentFragment(data);
      updateQueueStatus(`文档已更新 · ${nextSnapshot} · 已保留当前审阅位置`);
    } else {
      updateQueueStatus();
    }
  } catch (error) {
    updateQueueStatus('SERVER ERROR · 等待重新连接');
  } finally {
    state.polling = false;
  }
}

function bindReviewTargets() {
  document.querySelectorAll('.review-item:not([data-bound])').forEach(item => {
    item.dataset.bound = 'true';
    item.addEventListener('click', () => setCurrentRevision(item.dataset.rid, true));
  });
  document.querySelectorAll('.comment-item:not([data-bound])').forEach(item => {
    item.dataset.bound = 'true';
    item.addEventListener('click', () => setCurrentComment(item.dataset.cid, true));
  });
  document.querySelectorAll('.comment-anchor:not([data-bound])').forEach(marker => {
    marker.dataset.bound = 'true';
    marker.addEventListener('click', () => { setTab('comments'); setCurrentComment(marker.dataset.cid, true); });
  });
  revisionMarks = [...document.querySelectorAll('.revision-mark')];
  revisionMarks.filter(mark => !mark.dataset.bound).forEach(mark => {
    mark.dataset.bound = 'true';
    mark.addEventListener('click', event => { event.stopPropagation(); setTab('revisions'); state.filter = 'all'; applyFilter(); setCurrentRevision(mark.dataset.rid, false); });
    mark.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setTab('revisions'); state.filter = 'all'; applyFilter(); setCurrentRevision(mark.dataset.rid, false); } });
  });
  if (isMobileViewport()) {
    rulerFrame = 0;
    renderMobileRuler();
  }
}
function syncTopbarHeight() {
  if (!topbar) return;
  document.documentElement.style.setProperty('--topbar-height', `${topbar.getBoundingClientRect().height}px`);
  scheduleMobileRuler();
}
syncTopbarHeight();
if (topbar && 'ResizeObserver' in window) new ResizeObserver(syncTopbarHeight).observe(topbar);
window.addEventListener('resize', syncTopbarHeight);
window.addEventListener('scroll', scheduleMobileRuler, { passive: true });
bindReviewTargets();
mobileRulerTrack?.addEventListener('pointerdown', beginRulerDrag);
mobileRulerTrack?.addEventListener('pointermove', moveRulerDrag);
mobileRulerTrack?.addEventListener('pointerup', endRulerDrag);
mobileRulerTrack?.addEventListener('pointercancel', endRulerDrag);
mobileRulerViewport?.addEventListener('keydown', handleRulerKeydown);
document.addEventListener('pointerdown', event => {
  const hasSurface = Boolean(state.currentRid || state.currentCid || !commentCompose.hidden || !adjustCompose.hidden || !commentDetail.hidden || !selectionTools.hidden);
  if (hasSurface && !isReviewSurfaceTarget(event.target)) dismissReviewSurface();
}, true);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && (state.currentRid || state.currentCid || !commentCompose.hidden || !adjustCompose.hidden || !commentDetail.hidden || !selectionTools.hidden)) {
    event.preventDefault();
    dismissReviewSurface();
  }
});
document.querySelectorAll('.rail-tab').forEach(button => button.addEventListener('click', () => setTab(button.dataset.tab)));
document.querySelectorAll('.filter-button').forEach(button => button.addEventListener('click', () => { state.filter = button.dataset.filter; applyFilter(); }));
document.querySelectorAll('.view-button').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
actionButtons.forEach(button => button.addEventListener('click', () => { state.action = button.dataset.action; actionButtons.forEach(other => other.classList.toggle('is-selected', other === button)); detailError.textContent = ''; }));
previousReviewButton.addEventListener('click', () => jumpRevision(-1));
nextReviewButton.addEventListener('click', () => jumpRevision(1));
document.getElementById('decision-apply').addEventListener('click', applyDecision);
document.getElementById('comment-save').addEventListener('click', saveComment);
document.getElementById('comment-cancel').addEventListener('click', cancelComment);
document.getElementById('comment-detail-close').addEventListener('click', dismissReviewSurface);
document.getElementById('adjust-save').addEventListener('click', saveAdjustment);
document.getElementById('adjust-cancel').addEventListener('click', cancelAdjustment);
document.getElementById('export').addEventListener('click', exportDecisions);
sendButton.addEventListener('click', dispatchToAgent);
document.getElementById('comment-selection').addEventListener('mousedown', event => event.preventDefault());
document.getElementById('adjust-selection').addEventListener('mousedown', event => event.preventDefault());
document.getElementById('comment-selection').addEventListener('click', openCommentComposer);
document.getElementById('adjust-selection').addEventListener('click', openAdjustmentComposer);
document.addEventListener('selectstart', event => {
  const target = selectionParent(event.target);
  if (!target?.closest('.document-paper')) event.preventDefault();
}, true);
document.addEventListener('selectionchange', () => {
  window.clearTimeout(selectionCaptureTimer);
  selectionCaptureTimer = window.setTimeout(captureSelection, 32);
});
setView('markup'); setTab('revisions'); if (isMobileViewport()) setMobileSheet(null); updateStats(); loadQueue();
if (serverMode) window.setInterval(pollDocument, 2200);
""".replace("__BOOT__", boot_json)

    template = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>文档审阅 · DOCX2TYPED</title><meta name="description" content="DOCX2TYPED academic document review console">
<style>__CSS__</style></head>
<body data-view="markup">
<div class="page-frame">
  <div class="topbar" id="topbar">
    <header class="console-header">
      <div>
        <p class="brand-overline">DOCX2TYPED / ACADEMIC REVIEW</p>
        <h1>文档审阅</h1>
        <p>在保留原始格式的前提下，逐项核对修订与批注。</p>
      </div>
      <div class="header-actions">
        <span class="server-status" id="server-status">__SERVER_LABEL__</span>
        <div class="view-switch" role="group" aria-label="文档视图">
          <button class="view-button" data-view="markup" aria-pressed="true">修订</button>
          <button class="view-button" data-view="final" aria-pressed="false">最终</button>
          <button class="view-button" data-view="original" aria-pressed="false">原文</button>
        </div>
        <button class="send-action" id="send-agent">__SEND_LABEL__</button>
        <button class="primary-action" id="export">导出决策</button>
      </div>
    </header>
    <div class="header-rule"><span>当前文件：<strong>__DOCUMENT_TITLE__</strong></span><span id="global-status" aria-live="polite">__SUMMARY__</span></div>
  </div>
  <div class="selection-tools" id="selection-tools" hidden><span>已选择 <strong id="selection-count">0 字</strong></span><button id="adjust-selection">调整</button><button id="comment-selection">添加批注</button></div>
  <div class="selection-highlight" id="selection-highlight" hidden aria-hidden="true"></div>
  <nav class="mobile-ruler" id="mobile-ruler" aria-label="文档修订定位">
    <div class="mobile-ruler-track" id="mobile-ruler-track">
      <button class="mobile-ruler-viewport" id="mobile-ruler-viewport" type="button" role="slider" tabindex="0" aria-label="文档位置" aria-orientation="vertical" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-valuetext="文档开头"></button>
    </div>
  </nav>
  <nav class="review-jump-controls" id="review-jump-controls" aria-label="修订导航" hidden>
    <button class="review-jump-button" id="previous-review" type="button" aria-label="上一处修订" title="上一处修订">↑</button>
    <span class="review-jump-status" id="review-jump-status" aria-live="polite">0 / 0</span>
    <button class="review-jump-button" id="next-review" type="button" aria-label="下一处修订" title="下一处修订">↓</button>
  </nav>
  <div class="workspace">
    <main class="document-stage" aria-label="文档正文">
      <div class="stage-heading"><p class="eyebrow">DOCUMENT / READING STAGE</p><h2>__DOCUMENT_TITLE__</h2><p>字体、字号、上下标、修订层级与批注位置按源文档保留。</p></div>
      <article class="document-paper">__PARAGRAPHS__</article>
      <details class="format-diagnostics"><summary>格式诊断 <span class="diagnostic-status __AUDIT_CLASS__">__AUDIT_STATUS__</span></summary><div class="audit-wrap"><table class="audit-table"><thead><tr><th>Style</th><th>语义标签</th><th>已映射</th><th>待核对</th></tr></thead><tbody>__AUDIT_ROWS__</tbody></table></div></details>
    </main>
    <aside class="review-rail" id="review-rail" aria-label="审阅索引">
      <div class="rail-heading"><p class="eyebrow">REVIEW INDEX</p><h2>逐项审阅</h2><div class="rail-summary"><strong id="decided-count">00</strong><span id="rail-progress">0 / 0 已决策</span></div></div>
      <div class="rail-tabs" role="tablist" aria-label="审阅内容"><button class="rail-tab" data-tab="revisions" role="tab" aria-controls="revision-list" aria-selected="true">修订 <span class="tab-count">__REVISION_COUNT__</span></button><button class="rail-tab" data-tab="comments" role="tab" aria-controls="comment-list" aria-selected="false">批注 <span class="tab-count">__COMMENT_COUNT__</span></button></div>
      <div class="rail-filter" id="revision-filter"><span>显示</span><div class="filter-buttons"><button class="filter-button" data-filter="all" aria-pressed="true">全部</button><button class="filter-button" data-filter="pending" aria-pressed="false">待审</button><button class="filter-button" data-filter="decided" aria-pressed="false">已决策</button></div></div>
      <div class="review-list rail-list" id="revision-list">__REVISION_ITEMS__</div>
      <div class="review-list rail-list comment-list" id="comment-list" hidden>
        <section class="comment-section" data-source="word">
          <h3 class="comment-section-title">原文批注 <span>WORD COMMENTS</span></h3>
          <div id="word-comment-items">__COMMENT_ITEMS__</div>
        </section>
        <section class="comment-section" data-source="agent">
          <h3 class="comment-section-title">审阅批注 <span>REVIEW NOTES</span></h3>
          <div id="agent-comment-items"><p class="rail-empty">尚无新增批注</p></div>
        </section>
      </div>
      <div class="comment-compose" id="comment-compose" hidden>
        <p class="decision-kicker">NEW COMMENT / AGENT NOTE</p><h3 class="comment-compose-title">给 agent 的新批注</h3><p class="comment-compose-quote" id="comment-quote"></p><p class="comment-compose-meta" id="comment-meta"></p>
        <label class="mono" for="comment-note">批注内容</label><textarea class="decision-note" id="comment-note" placeholder="说明要改什么、为什么改，或需要 agent 核对什么"></textarea><p class="decision-error" id="comment-error" aria-live="polite"></p>
        <div class="comment-compose-actions"><button class="comment-cancel" id="comment-cancel">取消</button><button class="comment-save" id="comment-save">暂存批注</button></div>
      </div>
      <section class="decision-panel" id="decision-panel" aria-live="polite">
        <div class="decision-empty" id="decision-empty">从右侧索引选择一条修订，正文会自动定位到对应句子。</div>
        <div class="decision-content" id="decision-content" hidden>
          <p class="decision-kicker" id="decision-kicker"></p><h3 class="decision-title" id="decision-title"></h3><p class="decision-quote" id="decision-quote"></p><p class="decision-meta" id="decision-meta"></p>
          <div class="decision-actions"><button class="decision-action" data-action="accept">接受</button><button class="decision-action" data-action="reject">拒绝</button><button class="decision-action" data-action="defer">暂缓</button></div>
          <label class="mono" for="decision-note">审阅意见（可选）</label><textarea class="decision-note" id="decision-note" placeholder="把需要回传给 agent 的意见写在这里"></textarea><p class="decision-error" id="decision-error" aria-live="polite"></p><button class="decision-apply" id="decision-apply">保存本项决策</button>
        </div>
        <div class="adjust-compose" id="adjust-compose" hidden>
          <p class="decision-kicker">HUMAN PATCH / SOURCE ANCHORED</p><h3 class="adjust-compose-title">调整选中文本</h3><p class="adjust-compose-quote" id="adjust-quote"></p><p class="adjust-compose-meta" id="adjust-meta"></p>
          <label class="mono" for="adjust-text">调整后文本</label><textarea class="decision-note" id="adjust-text"></textarea><p class="decision-error" id="adjust-error" aria-live="polite"></p>
          <div class="adjust-compose-actions"><button class="adjust-cancel" id="adjust-cancel">取消</button><button class="adjust-save" id="adjust-save">暂存调整</button></div>
        </div>
        <div class="comment-detail" id="comment-detail" hidden>
          <p class="decision-kicker" id="comment-detail-kicker">WORD COMMENT</p><h3 class="comment-detail-title" id="comment-detail-title">批注</h3><p class="comment-detail-quote" id="comment-detail-quote"></p><p class="comment-detail-meta" id="comment-detail-meta"></p><p class="comment-detail-text" id="comment-detail-text"></p>
          <button class="comment-detail-close" id="comment-detail-close" type="button">关闭批注</button>
        </div>
      </section>
  </div>
</div>
<script>__JS__</script>
</body></html>"""
    return (
        template.replace("__CSS__", css)
        .replace("__JS__", js)
        .replace("__SERVER_LABEL__", "LOCAL SERVER" if server_mode else "OFFLINE PREVIEW")
        .replace("__SEND_LABEL__", "发送给 agent" if server_mode else "导出给 agent")
        .replace("__DOCUMENT_TITLE__", _ESCAPE(document_title))
        .replace("__SUMMARY__", _ESCAPE(summary))
        .replace("__PARAGRAPHS__", paragraph_html)
        .replace("__AUDIT_CLASS__", "has-warning" if warning_count else "")
        .replace("__AUDIT_STATUS__", _ESCAPE(audit_status))
        .replace("__AUDIT_ROWS__", audit_rows)
        .replace("__REVISION_COUNT__", str(len(ctx.revision_records)))
        .replace("__COMMENT_COUNT__", str(len(ctx.comment_records)))
        .replace("__REVISION_ITEMS__", revision_items)
        .replace("__COMMENT_ITEMS__", comment_items)
    )


def render_html(workdir: Path, *, server_mode: bool = False) -> str:
    typed_text = (workdir / "typed.md").read_text(encoding="utf-8")
    document = parse_typed(typed_text)
    parts = _template_parts(workdir)
    styles, infos, _ = _load_styles(workdir, parts)
    comments_meta = _comments_meta(parts)
    revision_keys = _revision_keys(workdir)
    return _build_page(
        workdir,
        document,
        styles,
        infos,
        comments_meta,
        revision_keys,
        server_mode=server_mode,
    )



def render_document_fragment(workdir: Path) -> dict[str, object]:
    """Render the replaceable document/index fragment for live server updates."""
    document = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
    parts = _template_parts(workdir)
    styles, infos, _ = _load_styles(workdir, parts)
    comments_meta = _comments_meta(parts)
    revision_keys = _revision_keys(workdir)
    ctx = RenderContext(infos, comments_meta, revision_keys)
    paragraph_html = "".join(_paragraph_html(paragraph, ctx) for paragraph in document.paragraphs)
    for comment_id, meta in comments_meta.items():
        if comment_id not in ctx._comment_seen:
            ctx.comment_records.append(
                {
                    "cid": comment_id,
                    "author": meta.get("author", ""),
                    "date": meta.get("date", ""),
                    "text": meta.get("text", ""),
                    "pid": "",
                    "order": str(len(ctx.comment_records) + 1),
                }
            )
    return {
        "html": paragraph_html,
        "revisions": ctx.revision_records,
        "comments": ctx.comment_records,
        "revision_items": "".join(_review_item_html(record) for record in ctx.revision_records)
        or '<div class="rail-empty">当前文档没有可审阅修订。</div>',
        "comment_items": "".join(_comment_item_html(record) for record in ctx.comment_records)
        or '<div class="rail-empty">当前文档没有批注。</div>',
    }
def generate(workdir: Path, output: Path, *, server_mode: bool = False) -> Path:
    page = render_html(workdir, server_mode=server_mode)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8", newline="\n")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)
    out = generate(args.workdir, args.output)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
