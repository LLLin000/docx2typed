"""HTML revision review console generator (prototype).

Consumes a docx2typed workdir and emits a self-contained HTML page for
human-in-the-loop review of tracked revisions:

- full document with real formatting (fonts/size/bold/superscript from
  styles.json rPr -> CSS, rStyle expanded from the template styles.xml)
- revisions rendered in place (delete = strikethrough, insert = highlight)
  with click-to-decide cards (accept / reject / comment)
- comment anchors with bubbles (text/author from the template comments.xml)
- original/final view toggle
- one-click export of decisions as JSON for the agent's second pass
- a format-coverage audit panel: every style id with its mapped/unmapped
  rPr features, so nothing is silently dropped

Usage: python -m scripts.review_console <workdir> -o <out.html>
"""
from __future__ import annotations

import argparse
import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any

_ESCAPE = html.escape

# ---------------------------------------------------------------- rPr -> CSS

_HIGHLIGHT_HEX = {
    "black": "#000000", "blue": "#0000FF", "cyan": "#00FFFF", "green": "#00FF00",
    "magenta": "#FF00FF", "red": "#FF0000", "yellow": "#FFFF00", "white": "#FFFFFF",
    "darkBlue": "#00008B", "darkCyan": "#008B8B", "darkGreen": "#006400",
    "darkMagenta": "#8B008B", "darkRed": "#8B0000", "darkYellow": "#808000",
    "darkGray": "#A9A9A9", "lightGray": "#D3D3D3", "none": "",
}


def rpr_to_css(style_rec: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Map a styles.json style record's rPr features to CSS. Returns the
    mapped declarations plus the list of features left unmapped."""
    features = style_rec.get("features", {})
    css: dict[str, str] = {}
    unmapped: list[str] = []

    ascii_font = features.get("font:ascii")
    ea_font = features.get("font:eastAsia")
    if ascii_font or ea_font:
        families = [f"'{f}'" for f in (ascii_font, ea_font) if f]
        css["font-family"] = ", ".join(families + ["sans-serif"])
    else:
        unmapped.append("font(absent)")

    sz = features.get("sz")
    if sz:
        try:
            css["font-size"] = f"{int(sz) / 2}pt"
        except (TypeError, ValueError):
            unmapped.append(f"sz={sz!r}")
    else:
        unmapped.append("sz(absent)")

    if features.get("b"):
        css["font-weight"] = "bold"
    if features.get("i"):
        css["font-style"] = "italic"
    if features.get("u"):
        css["text-decoration"] = "underline"
    if features.get("strike"):
        css["text-decoration"] = "line-through"

    vert = features.get("vertAlign")
    if vert in ("superscript", "subscript"):
        css["vertical-align"] = {"superscript": "super", "subscript": "sub"}[vert]
        css["font-size"] = "75%"
    elif vert:
        unmapped.append(f"vertAlign={vert!r}")

    color = features.get("color")
    if color:
        if color.startswith("#") or re.fullmatch(r"[0-9A-Fa-f]{6}", color):
            css["color"] = f"#{color.lstrip('#')}"
        else:
            unmapped.append(f"color={color!r} (theme)")

    highlight = features.get("highlight")
    if highlight:
        if highlight in _HIGHLIGHT_HEX:
            css["background-color"] = _HIGHLIGHT_HEX[highlight]
        else:
            unmapped.append(f"highlight={highlight!r}")

    kern = features.get("kern")
    if kern:
        css["letter-spacing"] = f"{int(kern) / 2}pt"

    # non-rendering or out-of-scope features
    for key in ("font:cs", "font:hAnsi", "font:hint", "rStyle", "szCs"):
        if key in features:
            pass  # handled via ascii/eastAsia or not needed for display

    return css, sorted(unmapped)


# ------------------------------------------------------------- template reads


def _template_parts(workdir: Path) -> dict[str, bytes]:
    template = workdir / "_template.docx"
    with zipfile.ZipFile(template) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _rstyle_definitions(parts: dict[str, bytes]) -> dict[str, str]:
    """Character style id -> rPr XML, from the template's styles.xml."""
    xml = parts.get("word/styles.xml", b"")
    if not xml:
        return {}
    text = xml.decode("utf-8", errors="replace")
    out: dict[str, str] = {}
    for match in re.finditer(
        r'<w:style\b[^>]*?w:type="character"[^>]*?w:styleId="([^"]+)"[^>]*>(.*?)</w:style>',
        text, re.S,
    ):
        style_id, body = match.group(1), match.group(2)
        rpr = re.search(r"<w:rPr[^>]*>(.*?)</w:rPr>", body, re.S)
        if rpr:
            out[style_id] = rpr.group(1)
    return out


def _rpr_features(rpr_xml: str) -> dict[str, str]:
    """Minimal rPr -> features for rStyle expansion (font/sz/b/i/color/vert)."""
    features: dict[str, str] = {}
    font = re.search(r"<w:rFonts\b[^>]*/?>", rpr_xml)
    if font:
        for attr in ("ascii", "eastAsia", "hAnsi", "cs", "hint"):
            m = re.search(rf'w:{attr}="([^"]*)"', font.group(0))
            if m:
                features[f"font:{attr}"] = m.group(1)
    sz = re.search(r'<w:sz\b[^>]*w:val="(\d+)"', rpr_xml)
    if sz:
        features["sz"] = sz.group(1)
    if re.search(r"<w:b\b", rpr_xml) and not re.search(r'<w:b\b[^>]*w:val="(?:0|false)"', rpr_xml):
        features["b"] = "1"
    if re.search(r"<w:i\b", rpr_xml) and not re.search(r'<w:i\b[^>]*w:val="(?:0|false)"', rpr_xml):
        features["i"] = "1"
    vert = re.search(r'<w:vertAlign\b[^>]*w:val="(superscript|subscript)"', rpr_xml)
    if vert:
        features["vertAlign"] = vert.group(1)
    color = re.search(r'<w:color\b[^>]*w:val="([^"]*)"', rpr_xml)
    if color:
        features["color"] = color.group(1)
    return features


def _comments_meta(parts: dict[str, bytes]) -> dict[str, dict[str, str]]:
    xml = parts.get("word/comments.xml", b"").decode("utf-8", errors="replace")
    out: dict[str, dict[str, str]] = {}
    for match in re.finditer(
        r'<w:comment\s+[^>]*?w:id="(\d+)"[^>]*>(.*?)</w:comment>', xml, re.S,
    ):
        tag = match.group(0)
        author = re.search(r'w:author="([^"]*)"', tag)
        date = re.search(r'w:date="([^"]*)"', tag)
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", match.group(2)))
        out[match.group(1)] = {
            "author": author.group(1) if author else "",
            "date": date.group(1) if date else "",
            "text": text,
        }
    return out


def _revision_keys(workdir: Path) -> dict[str, str]:
    """w_id -> revision_key from revisions.json (for decision export)."""
    try:
        inv = json.loads((workdir / "revisions.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        str(rev.get("w_id")): rev.get("revision_key", "")
        for rev in inv.get("revisions", []) if rev.get("w_id") is not None
    }


# ------------------------------------------------------------------ renderer


class Paragraph:
    def __init__(self, paragraph_id: str, body: str, base_style: str):
        self.paragraph_id = paragraph_id
        self.body = body
        self.base_style = base_style


def _parse_paragraphs(typed_text: str) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    for block in re.split(r"(?=<!--@p id=)", typed_text):
        m = re.match(r'<!--@p id="([^"]+)"[^>]*base="([^"]*)"[^>]*-->', block)
        if not m:
            continue
        body = block[m.end():].strip()
        paragraphs.append(Paragraph(m.group(1), body, m.group(2)))
    return paragraphs


_TOKEN_RE = re.compile(
    r"<docx-(?:anchor|inline|opaque)\b[^>]*/>"
    r"|<docx-revision\b[^>]*>.*?</docx-revision>"
    r"|<span data-s=\"[^\"]*\">.*?</span>"
    r"|[^<]+",
    re.S,
)


def _render_body(
    body: str,
    css_map: dict[str, str],
    rev_keys: dict[str, str],
    comments_meta: dict[str, dict[str, str]],
) -> str:
    out: list[str] = []
    for token in _TOKEN_RE.findall(body):
        if token.startswith("<span"):
            sid = re.search(r'data-s="([^"]*)"', token).group(1)
            inner = re.sub(r"<span[^>]*>|</span>", "", token)
            out.append(f'<span class="s-{sid}">{_ESCAPE(inner)}</span>')
        elif token.startswith("<docx-revision"):
            tag = token.split(">", 1)[0]
            kind = re.search(r'kind="([^"]*)"', tag).group(1)
            rid = re.search(r'id="([^"]*)"', tag).group(1)
            w_id = re.search(r'w:id="(\d+)"', tag)
            author = re.search(r'w:author="([^"]*)"', tag)
            date = re.search(r'w:date="([^"]*)"', tag)
            text = re.sub(r"<[^>]+>", "", token)  # strip nested span/docx tags
            key = rev_keys.get(w_id.group(1) if w_id else "", "")
            attrs = (
                f'data-rid="{rid}" data-kind="{kind}" data-key="{_ESCAPE(key)}"'
                f' data-author="{_ESCAPE(author.group(1) if author else "")}"'
                f' data-date="{_ESCAPE(date.group(1) if date else "")}"'
                f' data-text="{_ESCAPE(text)}"'
            )
            if kind == "delete":
                out.append(
                    f'<del class="rev rev-delete" {attrs}><span class="rev-text">{_ESCAPE(text)}</span></del>'
                )
            else:
                out.append(
                    f'<ins class="rev rev-insert" {attrs}><span class="rev-text">{_ESCAPE(text)}</span></ins>'
                )
        elif token.startswith("<docx-anchor"):
            tag = token
            kind = re.search(r'kind="([^"]*)"', tag)
            w_id = re.search(r'w:id="(\d+)"', tag)
            name = re.search(r'w:name="([^"]*)"', tag)
            kind_v = kind.group(1) if kind else "?"
            if kind_v == "comment-start" and w_id:
                meta = comments_meta.get(w_id.group(1), {})
                out.append(
                    f'<mark class="comment-anchor" data-wid="{w_id.group(1)}" '
                    f'title="{_ESCAPE(meta.get("author", ""))}: {_ESCAPE(meta.get("text", ""))}">'
                    f'💬</mark>'
                )
            elif kind_v == "comment-end" and w_id:
                out.append('<span class="comment-end"></span>')
            elif kind_v.startswith("bookmark") and name:
                out.append(f'<span class="bookmark" title="bookmark {_ESCAPE(name.group(1))}">🔖</span>')
            else:
                out.append(f'<span class="anchor-other" title="{_ESCAPE(kind_v)}">▫</span>')
        elif token.startswith("<docx-inline"):
            kind = re.search(r'kind="([^"]*)"', token)
            kind_v = kind.group(1) if kind else "?"
            if kind_v in ("br", "lastRenderedPageBreak", "tab", "cr"):
                out.append('<span class="inline-neutral"></span>')
            else:
                out.append(f'<span class="inline-other" title="{_ESCAPE(kind_v)}">▪</span>')
        elif token.startswith("<docx-opaque"):
            out.append('<span class="opaque" title="opaque structure">▧</span>')
        else:
            out.append(_ESCAPE(token))
    return "".join(out)


def _paragraph_html(
    para: Paragraph,
    css_map: dict[str, str],
    rev_keys: dict[str, str],
    comments_meta: dict[str, dict[str, str]],
    styles: dict[str, Any],
) -> str:
    audit = _style_audit(styles)
    body_html = _render_body(para.body, css_map, rev_keys, comments_meta)
    return (
        f'<section class="para" id="{para.paragraph_id}" data-pid="{para.paragraph_id}">'
        f'<div class="para-meta">{para.paragraph_id}'
        f'<span class="para-styles">{", ".join(audit)}</span></div>'
        f'<div class="para-body">{body_html}</div>'
        "</section>"
    )


def _style_audit(styles: dict[str, Any]) -> list[str]:
    return [f"s-{sid}" for sid in styles]


def _build_page(workdir: Path, paragraphs: list[Paragraph], styles: dict[str, Any], comments_meta: dict[str, dict[str, str]], rev_keys: dict[str, str]) -> str:
    css_rules: list[str] = []
    audit_rows: list[str] = []
    for sid, rec in styles.items():
        css, unmapped = rpr_to_css(rec)
        declarations = "; ".join(f"{k}: {v}" for k, v in css.items())
        css_rules.append(f".s-{sid} {{ {declarations} }}")
        audit_rows.append(
            f"<tr><td class='mono'>{sid}</td><td>{_ESCAPE(', '.join(sorted(rec.get('features', {}))))}</td>"
            f"<td>{_ESCAPE(', '.join(unmapped)) if unmapped else '—'}</td></tr>"
        )

    para_html = "".join(
        _paragraph_html(p, {}, rev_keys, comments_meta, styles)
        for p in paragraphs
    )

    page = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Revision review console</title>
<style>
:root {{ --ink:#1b2733; --line:#dde5ec; --del:#d64550; --ins:#1a9e6c; --bg:#f2f6f9; --card:#fff; }}
body {{ font-family: system-ui, sans-serif; color: var(--ink); margin: 0; background: var(--bg); }}
.wrap {{ max-width: 860px; margin: 0 auto; padding: 28px 20px 80px; }}
header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }}
header h1 {{ font-size: 20px; margin: 0; }}
.toolbar {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
.toolbar label {{ font-size: 13px; }}
.btn {{ background: #246bce; color: #fff; border: 0; border-radius: 6px; padding: 7px 14px; font-size: 13px; cursor: pointer; }}
.btn:hover {{ opacity: .9; }}
.para {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin-top: 12px; }}
.para-meta {{ font-size: 11px; color: #8a98a6; font-family: ui-monospace, monospace; margin-bottom: 6px; }}
.para-styles {{ margin-left: 8px; color: #b8c4ce; }}
.para-body {{ font-size: 14px; line-height: 1.8; }}
.para-body p {{ margin: 0; }}
.s-{{"s"}} {{ }}
{chr(10).join(css_rules)}
.rev {{ border-radius: 3px; padding: 0 2px; cursor: pointer; position: relative; }}
.rev-delete {{ background: #fdecee; color: var(--del); text-decoration: line-through; }}
.rev-insert {{ background: #e6f6ef; color: var(--ins); }}
.rev.pending {{ outline: 2px solid #d99a2b; }}
.rev.decided-accept {{ outline: 2px solid var(--ins); }}
.rev.decided-reject {{ outline: 2px solid var(--del); }}
.comment-anchor {{ background: #fff3bf; border-radius: 8px; padding: 0 3px; cursor: help; font-style: normal; }}
.inline-neutral, .inline-other, .anchor-other, .opaque {{ color: #b8c4ce; font-size: 11px; }}
.decision-card {{ position: fixed; right: 24px; top: 24px; width: 300px; background: var(--card); border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 12px 32px rgba(24,42,62,.15); padding: 16px; display: none; z-index: 10; }}
.decision-card h3 {{ margin: 0 0 8px; font-size: 14px; }}
.decision-card .meta {{ font-size: 12px; color: #5b6b7a; margin-bottom: 10px; word-break: break-all; }}
.decision-card .actions {{ display: flex; gap: 8px; margin-bottom: 10px; }}
.decision-card button {{ flex: 1; border: 1px solid var(--line); background: #fff; border-radius: 6px; padding: 6px; cursor: pointer; font-size: 13px; }}
.decision-card button:hover {{ background: #f2f6f9; }}
.decision-card textarea {{ width: 100%; box-sizing: border-box; min-height: 64px; border: 1px solid var(--line); border-radius: 6px; padding: 6px; font-size: 13px; }}
.decision-card .close {{ position: absolute; right: 10px; top: 8px; border: 0; background: none; font-size: 16px; cursor: pointer; color: #8a98a6; }}
#audit-panel {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; margin-top: 24px; padding: 14px 16px; }}
#audit-panel h3 {{ margin: 0 0 8px; font-size: 14px; }}
#audit-panel table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
#audit-panel td, #audit-panel th {{ border: 1px solid var(--line); padding: 4px 8px; text-align: left; }}
.mono {{ font-family: ui-monospace, monospace; }}
.stats {{ font-size: 12px; color: #5b6b7a; }}
</style></head><body>
<div class="wrap">
<header>
  <h1>Revision review console</h1>
  <div class="toolbar">
    <label><input type="checkbox" id="toggle-original"> 显示原文（含删除）</label>
    <span class="stats" id="stats"></span>
    <button class="btn" id="export">导出决策 JSON</button>
  </div>
</header>
{para_html}
<div id="audit-panel">
  <h3>格式覆盖审计（rPr → CSS 映射）</h3>
  <table><tr><th>style_id</th><th>features</th><th>未映射</th></tr>
  {chr(10).join(audit_rows)}
  </table>
</div>
</div>
<div class="decision-card" id="card">
  <button class="close" id="card-close">×</button>
  <h3 id="card-title">修订</h3>
  <div class="meta" id="card-meta"></div>
  <div class="actions">
    <button data-act="accept" class="act">✅ 接受</button>
    <button data-act="reject" class="act">❌ 拒绝</button>
  </div>
  <textarea id="card-comment" placeholder="意见（可选，将反馈给 agent）"></textarea>
  <div style="margin-top:8px"><button class="btn" id="card-apply" style="width:100%">应用此决策</button></div>
</div>
<script>
const decisions = {{}};
const revs = document.querySelectorAll('.rev');
let current = null;
const statsEl = document.getElementById('stats');

function refreshStats() {{
  const decided = Object.keys(decisions).length;
  statsEl.textContent = `${{revs.length}} 处修订 · ${{decided}} 已决策 · ${{revs.length - decided}} 待审`;
}}

revs.forEach(r => {{
  r.addEventListener('click', e => {{
    e.stopPropagation();
    current = r;
    document.getElementById('card-title').textContent =
      r.dataset.kind === 'delete' ? '删除修订' : '插入修订';
    document.getElementById('card-meta').textContent =
      `${{r.dataset.author}} · ${{r.dataset.date}}\\n段落 ${{r.closest('.para').dataset.pid}}\\n修订: «${{r.dataset.text}}»\\nkey: ${{r.dataset.key}}`;
    document.getElementById('card-comment').value = decisions[r.dataset.rid]?.comment || '';
    document.getElementById('card').style.display = 'block';
  }});
}});

document.querySelectorAll('.act').forEach(b => b.addEventListener('click', () => {{
  if (!current) return;
  current.dataset.action = b.dataset.act;
  current.classList.remove('pending', 'decided-accept', 'decided-reject');
  current.classList.add(b.dataset.act === 'accept' ? 'decided-accept' : 'decided-reject');
}}));

document.getElementById('card-apply').addEventListener('click', () => {{
  if (!current) return;
  const action = current.dataset.action || 'comment';
  decisions[current.dataset.rid] = {{
    revision_key: current.dataset.key,
    decision: action,
    comment: document.getElementById('card-comment').value.trim() || null,
  }};
  current.classList.add('decided-' + (action === 'accept' ? 'accept' : 'reject'));
  document.getElementById('card').style.display = 'none';
  current = null;
  refreshStats();
}});

document.getElementById('card-close').addEventListener('click', () => {{
  document.getElementById('card').style.display = 'none';
}});

document.getElementById('export').addEventListener('click', () => {{
  const payload = {{
    schema: 'docx2typed-review-decisions-1',
    workdir: {json.dumps(str(workdir))},
    decisions: Object.values(decisions),
  }};
  const blob = new Blob([JSON.stringify(payload, null, 2)], {{type: 'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'review-decisions.json';
  a.click();
}});

document.getElementById('toggle-original').addEventListener('change', e => {{
  document.body.classList.toggle('show-original', e.target.checked);
}});
</script>
<style>
body:not(.show-original) .rev-delete .rev-text {{ display: none; }}
</style>
</body></html>"""
    return page


def generate(workdir: Path, output: Path) -> Path:
    typed_text = (workdir / "typed.md").read_text(encoding="utf-8")
    styles = json.loads((workdir / "styles.json").read_text(encoding="utf-8"))["styles"]
    parts = _template_parts(workdir)
    comments_meta = _comments_meta(parts)
    rev_keys = _revision_keys(workdir)
    # rStyle expansion: merge character-style definitions into features
    rstyles = _rstyle_definitions(parts)
    for sid, rec in styles.items():
        rstyle_id = rec.get("features", {}).get("rStyle")
        if rstyle_id and rstyle_id in rstyles:
            for key, value in _rpr_features(rstyles[rstyle_id]).items():
                rec["features"].setdefault(key, value)
    paragraphs = _parse_paragraphs(typed_text)
    page = _build_page(workdir, paragraphs, styles, comments_meta, rev_keys)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
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
