"""Generate a readable, self-contained real-corpus demo HTML.

The page deliberately pairs every command with its observable effect:
input document -> one command -> measured output. The assets are the real
corpus run, not synthetic fixture screenshots.

Usage:
    python scripts/corpus_demo.py --png-dir D:/L/AppData/corpus-demo-work/png \
        --out D:/L/AppData/docx2typed-demo/corpus-demo.html
"""
from __future__ import annotations

import argparse
import base64
from datetime import date
import html
from pathlib import Path
from typing import Any

CASES: list[dict[str, Any]] = [
    {
        "number": "01",
        "eyebrow": "正文编辑 · 真实截图",
        "title": "标题真的变了：追加“（已核验）”",
        "copy": "真实 electrical-review.docx 的标题被改成“电刺激软骨缺损修复综述（已核验）”，不是合成文本。",
        "command": "docx2typed edit sync electrical-wd --no-track && docx2typed build electrical-wd --output after.docx",
        "output": [
            "P0 text: 电刺激软骨缺损修复综述 -> 电刺激软骨缺损修复综述（已核验）",
            "verify: PASS · 17-page DOCX opens in LibreOffice",
        ],
        "stats": [("1", "标题修改"), ("0", "残留修订"), ("17", "页"), ("PASS", "验证")],
        "text_edit": True,
        "image": ("before-electrical-review-proof.jpg", "after-electrical-review-proof.jpg"),
        "caption": "放大修改位置：左边原题，右边是实际输出中的“（已核验）”。拖动滑块。",
        "note": "这是正文内容真的改变，不是 XML 旁证；原文档布局仍可复现。",
    },
    {
        "number": "02",
        "eyebrow": "正文编辑 · 英文段落",
        "title": "英文标题从 Introduction 变成已验证版本",
        "copy": "真实 introduction-comments-0308.docx 的首个标题改为“Introduction — verified”，批注锚点仍保持合法。",
        "command": "docx2typed edit sync intro-wd --no-track && docx2typed build intro-wd --output after.docx",
        "output": [
            "P0 text: Introduction -> Introduction — verified",
            "verify: PASS · 7-page DOCX opens in LibreOffice",
        ],
        "stats": [("1", "标题修改"), ("3", "批注保留"), ("7", "页"), ("PASS", "验证")],
        "text_edit": True,
        "image": ("before-introduction-comments-0308-proof.jpg", "after-introduction-comments-0308-proof.jpg"),
        "caption": "真实英文引言首行的放大前后图；正文内容与批注结构同时存在。",
        "note": "文字编辑和批注锚点是两条独立路径，修改正文不会误删批注。",
    },
    {
        "number": "03",
        "eyebrow": "正文编辑 · 图注",
        "title": "图注标题也能直接编辑",
        "copy": "真实 mri-figure-notes.docx 的标题改为“MRI测量图注（已更新）”，下方医学图注和图像保持在原版面。",
        "command": "docx2typed edit sync mri-wd --no-track && docx2typed build mri-wd --output after.docx",
        "output": [
            "P0 text: MRI测量图注 -> MRI测量图注（已更新）",
            "verify: PASS · 31-page DOCX opens in LibreOffice",
        ],
        "stats": [("1", "标题修改"), ("1", "图像保留"), ("31", "页"), ("PASS", "验证")],
        "text_edit": True,
        "center_guide": True,
        "image": ("before-mri-figure-notes-proof.jpg", "after-mri-figure-notes-proof.jpg"),
        "caption": "真实 MRI 图注标题的放大前后图；虚线是原段落的居中轴，新增文字后中心不变。",
        "note": "标题保留 Word 的居中样式；文字变长时左右边界会重新计算，这是正常排版，不是标题锚点漂移。",
    },
    {
        "number": "04",
        "eyebrow": "正文编辑 · 整句改写",
        "title": "不是追加字符：完整句子被改写并重新排版",
        "copy": "真实 introduction-comments-0308.docx 的首句改写为包含“distributes mechanical loads during movement”的版本，后续段落随正文自然重排。",
        "command": "docx2typed edit sync intro-sentence-wd --no-track && docx2typed build intro-sentence-wd --output after.docx",
        "output": [
            "P1 sentence replaced; following text reflowed",
            "verify: PASS · 7-page DOCX opens in LibreOffice",
        ],
        "stats": [("1", "整句改写"), ("6", "行重排"), ("7", "页"), ("PASS", "验证")],
        "text_edit": True,
        "image": ("before-intro-sentence-proof.jpg", "after-intro-sentence-proof.jpg"),
        "caption": "放大真实段落：新增句意导致换行和行号一起变化，不是只贴一张 XML 图。",
        "note": "这才是 Word 编辑体验的核心：改文本，版面按文本重新流动。",
    },
    {
        "number": "05",
        "eyebrow": "修订落定",
        "title": "199 处修订 → 0 条残留",
        "copy": "57 MB 的真实投稿手稿，131 处插入、68 处删除。不是演示文件，是语料库里最重的一份。",
        "command": "docx2typed decide accept-all --workdir marked-wd --output after.docx",
        "output": [
            "settled 199 revisions via byte-level settlement",
            "new baseline revisions.json is empty",
        ],
        "stats": [("199", "落定"), ("131", "插入"), ("68", "删除"), ("0", "残留")],
        "image": ("before-marked-up-manuscript.jpg", "after-marked-up-manuscript.jpg"),
        "caption": "真实文档第 1 页：原件 / 落定后。拖动中间滑块。",
        "note": "你真正关心的结果：修订被处理掉，输出仍然能被 LibreOffice 打开。",
    },
    {
        "number": "06",
        "eyebrow": "表格结构",
        "title": "55 行 → 56 行",
        "copy": "full-draft-0703.docx 含 2 张表、340 个单元格。在 T0 的第 0 行后插入一整行空单元格。",
        "command": "docx2typed decide table-insert-row T0 --workdir tbl-wd --args 0 --workdir-out tbl-wd2",
        "output": [
            "table op applied: tbl-wd2",
            "<w:tr> count: 55 -> 56; new baseline verified",
        ],
        "stats": [("55", "原行数"), ("+1", "新行"), ("340", "单元格"), ("PASS", "验证")],
        "xml_before": '<w:tbl>\n  <w:tr>…第 0 行…</w:tr>\n  …其余 54 行…\n</w:tbl>',
        "xml_after": '<w:tbl>\n  <w:tr>…第 0 行…</w:tr>\n  <w:tr>…引擎合成的新空行…</w:tr>\n  …其余 54 行…\n</w:tbl>',
        "note": "只合成结构需要变化的那一行；原有单元格内容不被重写。",
        "image": ("before-full-draft-table.jpg", "after-full-draft-table.jpg"),
        "caption": "真实表格顶部：插行后出现一整行空单元格；下方 XML 证明行数 55 → 56。",
    },
    {
        "number": "07",
        "eyebrow": "字节保真",
        "title": "不改内容 → 0 字节漂移",
        "copy": "patent-1031.docx 的无编辑构建。document.xml 内容哈希相同，未触碰的 part 原样回放。",
        "command": "docx2typed build patent-wd --output noop.docx && docx2typed verify patent-wd noop.docx",
        "output": [
            "verify: PASS — 0 bytes differ",
            "sha256(document.xml): 6479cac7… == 6479cac7…",
        ],
        "stats": [("0", "字节漂移"), ("13", "part 原样"), ("32", "页专利"), ("PASS", "验证")],
        "proof": True,
        "note": "这就是‘不编辑时不乱动模板’：不是看起来差不多，是内容字节级一致。",
    },
]

CORPUS = [
    ("marked-up-manuscript", "57 MB", "288 段", "199 修订", "投稿手稿"),
    ("mri-figure-notes", "16 MB", "117 段", "78 修订 · 23 批注", "医学图注"),
    ("qmk-manual", "8.6 MB", "83 段", "键盘手册", "长文档"),
    ("electrical-review", "1.2 MB", "138 段", "5 表 · 152 格", "工程审阅"),
    ("patent-1031", "876 KB", "107 段", "6 批注", "专利"),
    ("supplementary-data", "852 KB", "10 段", "补充数据", "数据附件"),
    ("pharmacology-qa", "408 KB", "201 段", "9 表 · 148 格", "药理问答"),
    ("review-draft-0211", "172 KB", "231 段", "24 批注 · 1 表", "审稿草稿"),
    ("full-draft-0703", "80 KB", "106 段", "2 表 · 340 格", "完整草稿"),
    ("introduction-comments-0308", "28 KB", "16 段", "3 批注", "引言批注"),
]


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _img_data(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _stats(items: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<div class="stat"><strong>{_escape(value)}</strong><span>{_escape(label)}</span></div>'
        for value, label in items
    )
    return f'<div class="stats">{body}</div>'

REVIEW_COMMENT_SAMPLES: tuple[tuple[str, str], ...] = (
    ("0", "要写出英文版本的，中文的我们不投；标题也要重新命名过，这个标题过大了。"),
    ("2", "前面需要一个摘要，正文和小标题的写法需要重新修正。"),
    ("4", "从宏观层面再强调一下软骨所扮演的角色。"),
)

INTRO_COMMENT_SAMPLES: tuple[tuple[str, str], ...] = (
    ("1", "整体语言口语化，或者中式英语的风格非常严重，需要优化。"),
    ("4", "先介绍软骨在人体中扮演的角色，然后再介绍组成和结构。"),
    ("5", "相关文献应放在对应句旁，用特定颜色标记。"),
)


def _comment_item(comment_id: str, text: str, state: str = "open") -> str:
    removed = " comment-removed" if state == "removed" else ""
    label = "已删除" if state == "removed" else "批注"
    return f"""
        <div class="comment-item{removed}">
          <div class="comment-item-head"><span class="comment-mark"></span><b>id={_escape(comment_id)}</b><span>{label}</span></div>
          <p>{_escape(text)}</p>
        </div>"""


def _comment_sidebar(
    count: str,
    items: tuple[tuple[str, str], ...],
    *,
    empty_note: str | None = None,
    badges: tuple[tuple[str, str], ...] = (),
    more: int = 0,
) -> str:
    badge_html = "".join(
        f'<span class="comment-badge {kind}">{_escape(label)}</span>'
        for kind, label in badges
    )
    if empty_note is not None:
        body = f"""
          <div class="comment-empty">
            <strong>没有批注</strong>
            <span>{_escape(empty_note)}</span>
          </div>"""
    else:
        body = "".join(_comment_item(comment_id, text) for comment_id, text in items)
        if more:
            body += f'<div class="comment-more">+{more} 条真实批注</div>'
    return f"""
      <aside class="comment-sidebar">
        <div class="comment-sidebar-head"><b>批注</b><strong>{_escape(count)}</strong></div>
        <div class="comment-sidebar-sub">真实 comments.xml · 文档锚点</div>
        {f'<div class="comment-badges">{badge_html}</div>' if badge_html else ""}
        {body}
      </aside>"""


def _comment_stage(
    *,
    label: str,
    title: str,
    image_name: str,
    png_dir: Path,
    count: str,
    items: tuple[tuple[str, str], ...],
    empty_note: str | None = None,
    badges: tuple[tuple[str, str], ...] = (),
    more: int = 0,
) -> str:
    image = _img_data(png_dir / image_name)
    sidebar = _comment_sidebar(
        count,
        items,
        empty_note=empty_note,
        badges=badges,
        more=more,
    )
    return f"""
      <div class="comment-stage">
        <div class="comment-stage-head">
          <span class="stage-state">{_escape(label)}</span>
          <b>{_escape(title)}</b>
          <span class="stage-count">{_escape(count)} 条</span>
        </div>
        <div class="comment-surface">
          <div class="comment-page">
            <img src="{image}" alt="{_escape(title)} 正文页面">
            <span class="page-label">正文页面 · 页面像素对照</span>
          </div>
          {sidebar}
        </div>
      </div>"""


def _comment_decision_flow(png_dir: Path) -> str:
    return f"""
<section id="comment-lab" class="comment-lab">
  <div class="section-intro">
    <div><div class="section-kicker">批注决策 · 连续演示</div><h2>批注不是藏在 XML 里的数字</h2></div>
    <p>左侧是实际正文页面，右侧是从真实 comments.xml 提取的批注侧栏；命令执行后，页面、批注列表和 XML 结果一起对账。</p>
  </div>
  <div class="comment-callout"><b>你真正得到的优势</b><span>正文不被重写，批注可以按“全部清理”或“按 id 定点删除”，输出还有可独立复核的结构证据。</span></div>
  <article class="decision-row">
    <header class="decision-head">
      <div><span class="decision-number">01</span><div><b>全部清理</b><code>decide accept-all</code></div></div>
      <strong>24 <small>批注</small> → 0 <small>批注</small></strong>
    </header>
    <div class="decision-flow">
      {_comment_stage(label="输入", title="review-draft-0211.docx", image_name="before-review-draft-0211.jpg", png_dir=png_dir, count="24", items=REVIEW_COMMENT_SAMPLES, more=21)}
      <div class="decision-arrow">→</div>
      {_comment_stage(label="输出", title="after.docx · 新基线", image_name="after-review-draft-0211.jpg", png_dir=png_dir, count="0", items=(), empty_note="comments.xml: empty root · anchors: 0", badges=(("good", "verify PASS"),))}
    </div>
    <footer class="decision-foot"><code>docx2typed decide accept-all --workdir review-wd --output after.docx</code><span>comments.xml 24 → 0 · 文档锚点 24 → 0 · 页面正文保持不变</span></footer>
  </article>
  <article class="decision-row">
    <header class="decision-head">
      <div><span class="decision-number">02</span><div><b>只删一条</b><code>decide comment-delete 1</code></div></div>
      <strong>id=1 <small>删除</small> · id=4/5 <small>保留</small></strong>
    </header>
    <div class="decision-flow">
      {_comment_stage(label="输入", title="introduction-comments-0308.docx", image_name="before-introduction-comments-0308.jpg", png_dir=png_dir, count="3", items=INTRO_COMMENT_SAMPLES)}
      <div class="decision-arrow">→</div>
      {_comment_stage(label="输出", title="after.docx · 其余批注保留", image_name="after-introduction-comments-0308.jpg", png_dir=png_dir, count="2", items=INTRO_COMMENT_SAMPLES[1:], badges=(("removed", "id=1 removed"), ("good", "id=4/5 kept")))}
    </div>
    <footer class="decision-foot"><code>docx2typed decide comment-delete 1 --workdir intro-wd</code><span>只删除 id=1 · id=4、5 内容和锚点继续存在 · verify PASS</span></footer>
  </article>
</section>"""


def _workflow_html() -> str:
    return """
<section id="workflow" class="workflow">
  <div class="section-intro">
    <div><div class="section-kicker">一条完整链路</div><h2>从真实 DOCX 到可验证输出</h2></div>
    <p>所有基础操作共享同一个 workdir 和同一套验证门，不是各自孤立的脚本演示。</p>
  </div>
  <div class="workflow-track">
    <div class="workflow-step"><span>01</span><b>extract</b><code>input.docx → workdir/</code><small>typed.md · edit.md · format.json · 原模板</small></div>
    <div class="workflow-link">→</div>
    <div class="workflow-step"><span>02</span><b>edit / decide</b><code>正文 · 修订 · 批注 · 表格</code><small>只操作被授权的可编辑面</small></div>
    <div class="workflow-link">→</div>
    <div class="workflow-step"><span>03</span><b>build</b><code>workdir/ → output.docx</code><small>触碰范围重写，其余 part 原样回放</small></div>
    <div class="workflow-link">→</div>
    <div class="workflow-step"><span>04</span><b>verify</b><code>独立对账 → PASS</code><small>文本 · 样式 · 结构 · 包完整性 · LibreOffice</small></div>
  </div>
  <div class="workflow-run">
    <div class="workflow-run-head"><span>真实操作示例</span><b>introduction-comments-0308.docx</b><strong>一份 workdir，连续四步</strong></div>
    <div class="workflow-commands">
      <div><span>输入</span><code>docx2typed extract input.docx -o intro-wd</code><small>得到可读 edit.md 和不可变模板基线</small></div>
      <div><span>编辑</span><code>edit.md → edit sync</code><small>正文改写，批注锚点继续存在</small></div>
      <div><span>决策</span><code>comment-delete 1</code><small>只删 id=1，生成新基线</small></div>
      <div><span>交付</span><code>build → verify → LibreOffice</code><small>输出 DOCX 可打开，结果可复核</small></div>
    </div>
  </div>
  <div class="advantage-grid">
    <div><b>可编辑</b><span>普通文字保持可读，不必直接手改 XML。</span></div>
    <div><b>可决策</b><span>批注、修订、表格结构有明确的 CLI / MCP 操作。</span></div>
    <div><b>可验证</b><span>独立 verify 对账，未触碰内容保持字节级保护。</span></div>
  </div>
</section>"""


def _recipe(case: dict[str, Any]) -> str:
    command = _escape(case["command"])
    output = "\n".join(_escape(line) for line in case["output"])
    return f"""
      <div class="recipe">
        <div class="recipe-label"><span class="terminal-dot"></span>你只需要运行这一行</div>
        <pre class="command"><span class="prompt">$</span> {command}</pre>
        <div class="output-label">实际输出</div>
        <pre class="output">{output}</pre>
      </div>"""


def _xml_compare(case: dict[str, Any]) -> str:
    before = _escape(case["xml_before"])
    after = _escape(case["xml_after"])
    return f"""
      <div class="xml-compare">
        <div class="xml-panel before-panel">
          <div class="panel-head"><span class="signal red-signal"></span>输入：真实 XML</div>
          <pre>{before}</pre>
        </div>
        <div class="arrow">→</div>
        <div class="xml-panel after-panel">
          <div class="panel-head"><span class="signal green-signal"></span>输出：验证后的 XML</div>
          <pre>{after}</pre>
        </div>
      </div>"""


def _image_compare(case: dict[str, Any], png_dir: Path) -> str:
    before_name, after_name = case["image"]
    before = _img_data(png_dir / before_name)
    after = _img_data(png_dir / after_name)
    case_id = case["number"]
    compare_class = "compare text-compare" if case.get("text_edit") else "compare"
    guide = '<div class="center-guide"><span>居中轴</span></div>' if case.get("center_guide") else ""
    return f"""
      <div class="image-proof">
        <div class="image-head">
          <span class="label-before">原件 BEFORE</span>
          <input aria-label="拖动查看原件与输出" type="range" min="0" max="100" value="50" data-compare="compare-{case_id}">
          <span class="label-after">输出 AFTER</span>
        </div>
        <div class="{compare_class}" id="compare-{case_id}">
          <div class="compare-after" style="background-image:url('{after}')"></div>
          <div class="compare-before" style="background-image:url('{before}')"></div>
          {guide}
          <div class="compare-line"></div>
        </div>
        <div class="caption">{_escape(case['caption'])}</div>
      </div>"""


def _proof(case: dict[str, Any], png_dir: Path) -> str:
    parts: list[str] = []
    if case.get("image"):
        parts.append(_image_compare(case, png_dir))
    if case.get("xml_before"):
        parts.append(_xml_compare(case))
    if case.get("proof"):
        parts.append(
            """
      <div class="hash-proof">
        <div class="hash-row"><span>word/document.xml</span><b>6479cac7…</b></div>
        <div class="hash-eq">=</div>
        <div class="hash-row"><span>noop output</span><b>6479cac7…</b></div>
        <div class="hash-result">0 bytes differ</div>
      </div>"""
        )
    return "".join(parts)


def _case_html(case: dict[str, Any], png_dir: Path) -> str:
    result = _proof(case, png_dir)
    return f"""
    <article class="case" id="case-{case['number']}">
      <div class="case-top">
        <div class="case-number">{_escape(case['number'])}</div>
        <div>
          <div class="eyebrow">{_escape(case['eyebrow'])}</div>
          <h2>{_escape(case['title'])}</h2>
          <p class="case-copy">{_escape(case['copy'])}</p>
        </div>
      </div>
      <div class="case-grid">
        {_recipe(case)}
          <div class="result-label">可测量结果</div>
          {_stats(case['stats'])}
          {result}
          <p class="note">{_escape(case['note'])}</p>
        </div>
      </div>
    </article>"""


def _corpus_html() -> str:
    rows = []
    for name, size, paragraphs, features, kind in CORPUS:
        rows.append(
            f"""<div class="corpus-row">
              <div class="corpus-name"><b>{_escape(name)}.docx</b><span>{_escape(kind)}</span></div>
              <span>{_escape(size)}</span><span>{_escape(paragraphs)}</span><span>{_escape(features)}</span>
              <b class="pass">PASS</b>
            </div>"""
        )
    return "".join(rows)


def render(png_dir: Path) -> str:
    cases = "".join(_case_html(case, png_dir) for case in CASES)
    workflow = _workflow_html()
    comments = _comment_decision_flow(png_dir)
    generated = date.today().isoformat()
    corpus = _corpus_html()
    page = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>docx2typed · 真实语料库证据</title>
<style>
:root{--ink:#102033;--muted:#5c6b7a;--paper:#f4f6f8;--card:#fff;--navy:#0d1b2d;--line:#d8e0e8;--blue:#246bce;--cyan:#18a6c8;--red:#d94b5b;--green:#16865c;--yellow:#f1b438;--mono:"Cascadia Mono","SFMono-Regular",Consolas,monospace}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 system-ui,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:auto;padding:0 24px 72px}.hero{padding:32px 0 38px}.topline{display:flex;align-items:center;justify-content:space-between;gap:20px;font-size:13px;color:var(--muted)}.brand{font-weight:800;letter-spacing:-.02em;color:var(--ink)}.brand i{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--cyan);margin-right:8px}.topnav{display:flex;gap:16px}.topnav a{color:var(--muted);text-decoration:none}.topnav a:hover{color:var(--blue)}
.hero-grid{display:grid;grid-template-columns:1.15fr .85fr;gap:52px;align-items:end;margin-top:58px}.hero h1{font-size:clamp(42px,6vw,76px);line-height:1.02;letter-spacing:-.065em;margin:0;max-width:720px}.hero h1 em{font-style:normal;color:var(--blue)}.hero-lead{font-size:20px;line-height:1.5;color:var(--muted);max-width:620px;margin:24px 0 0}.hero-actions{display:flex;align-items:center;gap:14px;margin-top:28px}.primary{display:inline-block;background:var(--navy);color:#fff;border-radius:8px;padding:11px 17px;text-decoration:none;font-weight:700}.primary:hover{background:var(--blue)}.micro{font-size:13px;color:var(--muted)}
.proof-board{background:var(--navy);color:#fff;border-radius:18px;padding:24px;box-shadow:0 18px 40px rgba(13,27,45,.18);position:relative;overflow:hidden}.proof-board:after{content:"";position:absolute;width:220px;height:220px;border-radius:50%;background:rgba(24,166,200,.2);filter:blur(25px);right:-80px;top:-80px}.board-kicker{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#9ec7d3;position:relative}.board-flow{display:flex;align-items:center;gap:10px;margin-top:28px;position:relative}.board-flow strong{font-size:56px;line-height:1;letter-spacing:-.08em}.board-flow .arrow{color:#8bdbe8;font-size:28px}.board-flow small{display:block;color:#aab8c7;font-size:12px;line-height:1.35;margin-top:7px}.board-rule{height:1px;background:rgba(255,255,255,.14);margin:24px 0 17px}.board-bottom{display:flex;justify-content:space-between;color:#b3c2d0;font-size:12px;position:relative}.board-bottom b{color:#66e1ae}
.section-intro{display:flex;justify-content:space-between;align-items:end;gap:20px;margin:18px 0 22px}.section-intro h2{font-size:28px;letter-spacing:-.04em;margin:0}.section-intro p{color:var(--muted);margin:4px 0 0;max-width:620px}.section-kicker{font-size:12px;text-transform:uppercase;letter-spacing:.16em;color:var(--blue);font-weight:800}
.case{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;margin-top:18px;box-shadow:0 10px 28px rgba(24,42,62,.05)}.case-top{display:grid;grid-template-columns:54px 1fr;gap:18px}.case-number{width:42px;height:42px;border-radius:10px;background:#e5f3f7;color:var(--cyan);display:grid;place-items:center;font-weight:800;font-family:var(--mono)}.eyebrow{font-size:11px;letter-spacing:.14em;color:var(--blue);font-weight:800;text-transform:uppercase}.case h2{font-size:clamp(23px,3vw,34px);line-height:1.15;letter-spacing:-.045em;margin:3px 0 8px}.case-copy{color:var(--muted);max-width:850px;margin:0}.case-grid{display:grid;grid-template-columns:minmax(250px,.72fr) minmax(0,1.28fr);gap:24px;margin-top:24px;align-items:start}.recipe{border-radius:12px;background:#0b1727;color:#dbe7f2;overflow:hidden;min-width:0}.recipe-label,.output-label{font-size:12px;color:#9bb0c3;padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.11)}.recipe-label{display:flex;align-items:center;gap:8px}.terminal-dot{width:8px;height:8px;background:#61dc9a;border-radius:50%;box-shadow:0 0 10px #61dc9a}.command,.output{font:13px/1.65 var(--mono);white-space:pre-wrap;word-break:break-word;margin:0;padding:15px}.command{color:#fff}.prompt{color:#61dc9a;font-weight:800}.output-label{border-top:1px solid rgba(255,255,255,.11);border-bottom:0}.output{color:#9fb4c7;padding-top:11px;padding-bottom:16px}.result-label{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:800}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:10px 0 16px}.stat{border:1px solid var(--line);border-radius:9px;padding:8px 9px;min-width:0}.stat strong{display:block;font-size:21px;line-height:1.1;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.stat span{display:block;font-size:11px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.image-proof{border:1px solid var(--line);border-radius:11px;overflow:hidden;background:#eef2f5}.image-head{display:flex;align-items:center;gap:12px;padding:8px 11px;background:#fff;font-size:11px;font-weight:800;letter-spacing:.09em}.image-head input{flex:1;accent-color:var(--blue)}.label-before{color:var(--red)}.label-after{color:var(--green)}.compare{position:relative;height:450px;overflow:hidden;background:#e8edf1}.compare-before,.compare-after{position:absolute;inset:0;background-repeat:no-repeat;background-size:contain;background-position:top center}.compare-before{clip-path:inset(0 50% 0 0)}.compare-line{position:absolute;left:50%;top:0;bottom:0;width:2px;background:#fff;box-shadow:0 0 0 1px rgba(16,32,51,.22);pointer-events:none}.caption{font-size:12px;color:var(--muted);padding:8px 11px;background:#fff}.xml-compare{display:grid;grid-template-columns:1fr 24px 1fr;gap:10px;align-items:stretch}.xml-panel{border:1px solid var(--line);border-radius:10px;overflow:hidden;min-width:0}.panel-head{font-size:12px;color:var(--muted);padding:9px 11px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}.signal{width:8px;height:8px;border-radius:50%;display:inline-block}.red-signal{background:var(--red)}.green-signal{background:var(--green)}.xml-panel pre{font:12px/1.6 var(--mono);white-space:pre-wrap;word-break:break-word;padding:13px;margin:0;min-height:130px}.before-panel pre{color:#9e3e4c;background:#fff8f8}.after-panel pre{color:#16704f;background:#f3fcf7}.xml-compare>.arrow{display:grid;place-items:center;font-size:24px;color:var(--blue)}.hash-proof{border:1px solid #cce8db;background:#f3fcf7;border-radius:11px;padding:18px}.hash-row{display:flex;justify-content:space-between;gap:16px;font:13px var(--mono);padding:8px 0;border-bottom:1px solid #d8eee1}.hash-row b{color:var(--green)}.hash-eq{text-align:center;color:var(--green);font-size:22px;line-height:1.1;padding:5px}.hash-result{color:var(--green);font-weight:800;font-size:21px;text-align:center;padding-top:10px}.note{font-size:13px;color:var(--muted);margin:10px 0 0}
.corpus{margin-top:56px}.corpus-box{border:1px solid var(--line);border-radius:14px;background:#fff;overflow:hidden}.corpus-head,.corpus-row{display:grid;grid-template-columns:2.5fr .8fr .8fr 1.35fr .55fr;gap:14px;align-items:center;padding:12px 16px}.corpus-head{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);background:#edf2f5}.corpus-row{border-top:1px solid #edf0f2;font-size:13px}.corpus-row:hover{background:#f8fafb}.corpus-name b{display:block}.corpus-name span{display:block;font-size:11px;color:var(--muted);margin-top:1px}.pass{color:var(--green);font-size:12px;letter-spacing:.06em}.corpus-foot{display:flex;gap:10px;flex-wrap:wrap;margin-top:15px;color:var(--muted);font-size:13px}.corpus-foot b{color:var(--ink)}
.footer{margin-top:46px;padding-top:20px;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:18px;color:var(--muted);font-size:12px}.footer b{color:var(--ink)}
@media(max-width:860px){.hero-grid{grid-template-columns:1fr;gap:28px;margin-top:38px}.hero h1{font-size:54px}.case-grid{grid-template-columns:1fr}.compare{height:420px}.section-intro{display:block}.section-intro p{margin-top:8px}.corpus-head{display:none}.corpus-row{grid-template-columns:1fr auto;gap:4px 12px}.corpus-row>span:nth-of-type(1),.corpus-row>span:nth-of-type(2),.corpus-row>span:nth-of-type(3){font-size:12px}.corpus-row>span:nth-of-type(1){grid-column:2;grid-row:1}.corpus-row>span:nth-of-type(2){grid-column:1;grid-row:2}.corpus-row>span:nth-of-type(3){grid-column:1;grid-row:3}.corpus-row .pass{grid-column:2;grid-row:2 / span 2}.topnav{display:none}}
@media(max-width:560px){.wrap{padding:0 16px 50px}.hero{padding-top:22px}.hero-grid{margin-top:32px}.hero h1{font-size:43px}.hero-lead{font-size:17px}.hero-actions{align-items:flex-start;flex-direction:column}.case{padding:20px}.case-top{grid-template-columns:42px 1fr;gap:12px}.case-number{width:36px;height:36px}.case h2{font-size:26px}.stats{grid-template-columns:repeat(2,1fr)}.xml-compare{grid-template-columns:1fr;gap:8px}.xml-compare>.arrow{transform:rotate(90deg);height:16px}.compare{height:330px}.board-flow strong{font-size:45px}.footer{display:block}.footer span{display:block;margin-top:8px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
.hero-grid{grid-template-columns:1.28fr .72fr;gap:36px}
.hero h1{font-size:clamp(42px,5.5vw,68px)}
@media(max-width:860px){.hero-grid{grid-template-columns:1fr;gap:28px}.hero h1{font-size:54px}}
@media(max-width:560px){.hero h1{font-size:38px}}
.compare.text-compare{height:280px}
.text-compare .compare-before,.text-compare .compare-after{background-size:auto 100%}
.center-guide{position:absolute;left:50%;top:0;bottom:0;border-left:1px dashed rgba(36,107,206,.72);z-index:1;pointer-events:none}
.center-guide span{position:absolute;top:6px;left:6px;padding:1px 4px;border-radius:4px;background:rgba(36,107,206,.9);color:#fff;font:10px var(--mono);white-space:nowrap}
.compare-line{z-index:2}
.workflow{margin-top:8px}.workflow-track{display:grid;grid-template-columns:minmax(0,1fr) 24px minmax(0,1fr) 24px minmax(0,1fr) 24px minmax(0,1fr);gap:10px;align-items:stretch}.workflow-step{background:#fff;border:1px solid var(--line);border-radius:12px;padding:17px;min-height:142px}.workflow-step>span{display:block;color:var(--cyan);font:700 11px var(--mono);letter-spacing:.12em}.workflow-step>b{display:block;margin-top:7px;font-size:18px;letter-spacing:-.02em}.workflow-step code{display:block;margin-top:8px;color:var(--blue);font:12px/1.45 var(--mono);word-break:break-word}.workflow-step small{display:block;margin-top:10px;color:var(--muted);font-size:12px;line-height:1.45}.workflow-link{display:grid;place-items:center;color:var(--blue);font-size:25px}.workflow-run{margin-top:18px;border:1px solid var(--line);border-radius:14px;background:var(--navy);color:#fff;overflow:hidden;box-shadow:0 12px 28px rgba(13,27,45,.12)}.workflow-run-head{display:flex;align-items:center;gap:14px;padding:13px 17px;border-bottom:1px solid rgba(255,255,255,.12);font-size:12px}.workflow-run-head span{color:#9ec7d3;text-transform:uppercase;letter-spacing:.12em}.workflow-run-head b{font-family:var(--mono);font-weight:500}.workflow-run-head strong{margin-left:auto;color:#8bdbe8;font-size:12px}.workflow-commands{display:grid;grid-template-columns:repeat(4,1fr)}.workflow-commands>div{padding:17px;border-right:1px solid rgba(255,255,255,.12)}.workflow-commands>div:last-child{border-right:0}.workflow-commands span{display:block;color:#9ec7d3;font-size:11px;letter-spacing:.12em;text-transform:uppercase}.workflow-commands code{display:block;margin-top:8px;color:#fff;font:12px/1.45 var(--mono);word-break:break-word}.workflow-commands small{display:block;margin-top:10px;color:#aab8c7;font-size:12px;line-height:1.4}.advantage-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px}.advantage-grid>div{padding:15px 17px;border-left:3px solid var(--cyan);background:#eaf4f6}.advantage-grid b{display:block;font-size:15px}.advantage-grid span{display:block;margin-top:4px;color:var(--muted);font-size:13px;line-height:1.45}.comment-lab{margin-top:64px}.comment-callout{display:flex;gap:15px;align-items:baseline;padding:14px 17px;border:1px solid #b9dce4;border-radius:12px;background:#eef9fb}.comment-callout b{white-space:nowrap;color:#0d6d82}.comment-callout span{color:#476271;font-size:14px}.decision-row{margin-top:18px;padding:20px;border:1px solid var(--line);border-radius:16px;background:#fff;box-shadow:0 10px 28px rgba(24,42,62,.05)}.decision-head{display:flex;align-items:center;justify-content:space-between;gap:16px}.decision-head>div{display:flex;align-items:center;gap:12px}.decision-number{display:grid;place-items:center;width:34px;height:34px;border-radius:9px;background:#e5f3f7;color:var(--cyan);font:700 12px var(--mono)}.decision-head b{display:block;font-size:19px}.decision-head code{display:block;margin-top:2px;color:var(--muted);font:12px var(--mono)}.decision-head>strong{font-size:27px;letter-spacing:-.04em}.decision-head small{font-size:12px;font-weight:500;color:var(--muted)}.decision-flow{display:grid;grid-template-columns:minmax(0,1fr) 26px minmax(0,1fr);gap:12px;align-items:center;margin-top:18px}.decision-arrow{color:var(--blue);font-size:26px;text-align:center}.comment-stage{border:1px solid var(--line);border-radius:11px;overflow:hidden;background:#f8fafb}.comment-stage-head{display:flex;align-items:center;gap:9px;padding:9px 11px;background:#fff;border-bottom:1px solid var(--line);font-size:12px}.stage-state{padding:2px 6px;border-radius:4px;background:#fbeaec;color:var(--red);font:700 10px var(--mono)}.comment-stage:last-child .stage-state{background:#e6f5ee;color:var(--green)}.comment-stage-head b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.stage-count{margin-left:auto;color:var(--muted);font:11px var(--mono);white-space:nowrap}.comment-surface{display:grid;grid-template-columns:minmax(0,.88fr) minmax(220px,1.12fr);min-height:340px}.comment-page{position:relative;display:flex;align-items:center;justify-content:center;min-height:340px;padding:12px;background:#e9eef1;overflow:hidden}.comment-page img{display:block;max-width:100%;max-height:316px;object-fit:contain;box-shadow:0 5px 14px rgba(16,32,51,.14)}.page-label{position:absolute;left:10px;bottom:8px;padding:2px 5px;background:rgba(255,255,255,.9);color:var(--muted);font-size:10px}.comment-sidebar{padding:12px;background:#fbfcfd;border-left:1px solid var(--line);overflow:hidden}.comment-sidebar-head{display:flex;justify-content:space-between;align-items:baseline}.comment-sidebar-head b{font-size:16px}.comment-sidebar-head strong{color:var(--blue);font:700 18px var(--mono)}.comment-sidebar-sub{margin-top:2px;color:var(--muted);font-size:10px}.comment-item{padding:11px 0;border-top:1px solid #e8edf1}.comment-item:first-of-type{margin-top:10px}.comment-item-head{display:flex;align-items:center;gap:6px;color:var(--muted);font:11px var(--mono)}.comment-item-head b{color:var(--ink)}.comment-mark{width:7px;height:7px;border-radius:50%;background:var(--red)}.comment-item p{margin:5px 0 0;color:#354a59;font-size:12px;line-height:1.45}.comment-more{margin-top:6px;padding-top:9px;border-top:1px dashed var(--line);color:var(--blue);font-size:11px}.comment-empty{display:grid;place-items:center;min-height:235px;text-align:center;color:var(--green)}.comment-empty strong{font-size:18px}.comment-empty span{max-width:180px;margin-top:7px;color:var(--muted);font:11px/1.5 var(--mono)}.comment-badges{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}.comment-badge{padding:3px 6px;border-radius:4px;font:10px var(--mono)}.comment-badge.removed{background:#fbeaec;color:var(--red)}.comment-badge.good{background:#e6f5ee;color:var(--green)}.decision-foot{display:flex;justify-content:space-between;gap:16px;margin-top:12px;color:var(--muted);font-size:12px}.decision-foot code{color:var(--ink);font:11px var(--mono)}.decision-foot span{text-align:right}
@media(max-width:860px){.workflow-track{grid-template-columns:1fr}.workflow-link{height:24px;transform:rotate(90deg)}.workflow-commands{grid-template-columns:1fr 1fr}.comment-surface{grid-template-columns:minmax(0,.8fr) minmax(210px,1.2fr)}}
@media(max-width:560px){.workflow-run-head{display:block}.workflow-run-head strong{display:block;margin:7px 0 0}.workflow-commands{grid-template-columns:1fr}.workflow-commands>div{border-right:0;border-bottom:1px solid rgba(255,255,255,.12)}.workflow-commands>div:last-child{border-bottom:0}.advantage-grid{grid-template-columns:1fr}.comment-callout{display:block}.comment-callout span{display:block;margin-top:5px}.decision-head{display:block}.decision-head>strong{display:block;margin:12px 0 0 46px}.decision-flow{grid-template-columns:1fr}.decision-arrow{transform:rotate(90deg);height:24px}.comment-surface{grid-template-columns:1fr}.comment-page{min-height:230px}.comment-page img{max-height:215px}.comment-sidebar{border-left:0;border-top:1px solid var(--line)}.decision-foot{display:block}.decision-foot span{display:block;margin-top:6px;text-align:left}}
</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <div class="topline"><div class="brand"><i></i>docx2typed / 真实语料库证据</div><nav class="topnav"><a href="#workflow">看完整链路</a><a href="#comment-lab">看批注决策</a><a href="#cases">看证据索引</a></nav></div>
  <div class="hero-grid">
    <div>
      <h1>一份真实 DOCX，<br><em>四步走到可验证结果。</em></h1>
      <p class="hero-lead">不是把 Word 转成一张图：同一份真实文档先提取可编辑面，再处理正文、修订、批注和表格，最后原子构建并独立验证。</p>
      <div class="hero-actions"><a class="primary" href="#workflow">先看完整链路</a><span class="micro">一个 workdir · 4 个阶段 · 175 tests · 10/10 真实语料库</span></div>
    </div>
    <aside class="proof-board">
      <div class="board-kicker">一个 workdir / 四个连续阶段</div>
      <div class="board-flow"><div><strong>01</strong><small>extract<br>保留结构基线</small></div><div class="arrow">→</div><div><strong>04</strong><small>verify<br>输出可对账</small></div></div>
      <div class="board-rule"></div>
      <div class="board-bottom"><span>正文 · 批注 · 表格 · 修订</span><b>输出 PASS</b></div>
    </aside>
  </div>
</header>
{workflow}
{comments}
<section id="cases">
  <div class="section-intro"><div><div class="section-kicker">逐项证据索引</div><h2>把刚才的链路拆开复核</h2></div><p>上面先展示连续工作流和批注决策；这里按正文、修订、表格和字节保真逐项查看命令、输出与证据。</p></div>
  {cases}
</section>
<section id="corpus" class="corpus">
  <div class="section-intro"><div><div class="section-kicker">真实语料库</div><h2>这不是一个小样例</h2></div><p>10 份用户监督挑选的真实 DOCX，大小从 28 KB 到 57 MB；每份都跑过 extract → edit sync → build → verify。</p></div>
  <div class="corpus-box">
    <div class="corpus-head"><span>文件</span><span>大小</span><span>段落</span><span>结构 / 特征</span><span>结果</span></div>
    {corpus}
  </div>
  <div class="corpus-foot"><span>累计 <b>1,297</b> 个段落</span><span>发现 <b>56</b> 条批注</span><span>最大文档 <b>57 MB</b></span><span>全库 acceptance <b>10/10</b></span></div>
</section>
<footer class="footer"><b>docx2typed · typed-mode</b><span>175 tests passed · 10/10 corpus passed · 10/10 LibreOffice conversion passed · generated {generated}</span></footer>
</div>
<script>
document.querySelectorAll('[data-compare]').forEach(function(input){
  var box=document.getElementById(input.dataset.compare);
  var before=box.querySelector('.compare-before');
  var line=box.querySelector('.compare-line');
  function update(){
    var value=Number(input.value);
    before.style.clipPath='inset(0 '+value+'% 0 0)';
    line.style.left=(100-value)+'%';
  }
  input.addEventListener('input',update); update();
});
</script>
</body>
    </html>"""
    return page.replace("{workflow}", workflow).replace("{comments}", comments).replace("{cases}", cases).replace("{corpus}", corpus).replace("{generated}", generated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--png-dir", required=True)
    args = parser.parse_args(argv)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(Path(args.png_dir)), encoding="utf-8")
    print(f"wrote {output} ({output.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
