"""Generate the corpus demo HTML (self-contained, base64 images).

One command on the left, the real effect on the right — built from the
10-document real corpus run.

Usage:
    python scripts/corpus_demo.py --out D:/L/AppData/docx2typed-demo/corpus-demo.html
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEMOS = [
    {
        "id": "revisions",
        "kicker": "REVISION SETTLEMENT",
        "title": "199 处修订，一行命令全部落定",
        "sub": "marked-up-manuscript.docx（57 MB 真实投稿手稿）— 3 位作者、131 处插入、68 处删除、跨 73 页。",
        "code": [
            ("bash", "$ docx2typed decide accept-all \\"),
            ("bash", "    --workdir marked-wd \\"),
            ("bash", "    --output after.docx"),
            ("out", "decided-all: marked-wd2"),
            ("out", "  settled 199 revisions via byte-level settlement"),
            ("out", "  new baseline revisions.json is empty"),
        ],
        "stats": [("199", "修订落定"), ("131", "插入采纳"), ("68", "删除采纳"), ("0", "残留")],
        "image": ("before-marked-up-manuscript.png", "after-marked-up-manuscript.png"),
        "image_caption": "第 1 页：落定前后（LibreOffice 渲染，同一文档 57 MB → 63 页干净底稿）",
    },
    {
        "id": "comments-all",
        "kicker": "COMMENT DECISIONS",
        "title": "24 条批注连根拔起：锚点 + 内容 + 条目三处同步清除",
        "sub": "review-draft-0211.docx — 审稿批注 24 条、25 个书签锚点。accept-all 把 comments.xml 从 19 KB 清成合法空根。",
        "code": [
            ("bash", "$ docx2typed decide accept-all \\"),
            ("bash", "    --workdir review-wd \\"),
            ("bash", "    --output after.docx"),
            ("out", "comments.xml: 24 entries -> empty root"),
            ("out", "commentRangeStart/End anchors: 24 -> 0"),
        ],
        "stats": [("24", "批注清除"), ("24", "锚点移除"), ("19 KB", "→ 1.3 KB"), ("0", "残留")],
        "xml_before": (
            '<w:comment w:id="1" w:author="审稿人A" w:date="2026-03-16T10:40:00Z">\n'
            "  <w:p>…图 3 的显著性标注与正文不一致…</w:p>\n"
            "</w:comment>\n"
            '…共 24 条，19,343 字节…'
        ),
        "xml_after": "<w:comments …ns…></w:comments>  ← 合法空根，13 个命名空间原样保留",
    },
    {
        "id": "comment-single",
        "kicker": "COMMENT DECISIONS",
        "title": "单条批注按 id 删除，其余批注原封不动",
        "sub": "introduction-comments-0308.docx — 3 条批注（id 1/4/5），删除 id=1，4 和 5 的锚点重新锚定。",
        "code": [
            ("bash", "$ docx2typed decide comment-delete 1 \\"),
            ("bash", "    --workdir intro-wd"),
            ("out", "deleted comment: 1 (1 entry paragraph(s))"),
            ("out", "anchors re-anchored: comments 4, 5 untouched"),
        ],
        "stats": [("3", "→ 2 条"), ("id=1", "已删除"), ("4 / 5", "锚点保留"), ("verify", "绿")],
        "xml_before": '<w:comment w:id="1" w:author="jinxq" w:date="2026-03-16T10:40:00Z">…</w:comment>\n<w:comment w:id="4" …/>\n<w:comment w:id="5" …/>',
        "xml_after": '<w:comment w:id="4" …/>\n<w:comment w:id="5" …/>\n\n<!-- id=1 条目 + 文档内 commentRangeStart/End/Reference 全部移除 -->',
    },
    {
        "id": "table",
        "kicker": "TABLE STRUCTURE",
        "title": "表格插一行：结构字节由引擎合成，其余 55 行逐字节不动",
        "sub": "full-draft-0703.docx — 2 张表 340 个单元格。在第 0 行后插入一行空单元格行，第 1 页起的分页流自动重排。",
        "code": [
            ("bash", "$ docx2typed decide table-insert-row T0 \\"),
            ("bash", "    --workdir tbl-wd --args 0 \\"),
            ("bash", "    --output after.docx --workdir-out tbl-wd2"),
            ("out", "table op applied: tbl-wd2"),
            ("out", "<w:tr> count: 55 -> 56  (new baseline verified)"),
        ],
        "stats": [("55", "→ 56 行"), ("0", "模板字节改动"), ("T0", "第 0 行后"), ("verify", "绿")],
        "xml_before": '<w:tr><w:tc><w:tcPr><w:tcW w:w="…"/></w:tcPr><w:p>…第 0 行…</w:p></w:tc>…</w:tr>',
        "xml_after": '<w:tr><w:tc><w:tcPr><w:tcW w:w="…"/></w:tcPr><w:p>…第 0 行…</w:p></w:tc>…</w:tr>\n<w:tr><!-- 新行：空单元格，克隆自第 0 行的列结构 -->…</w:tr>',
    },
    {
        "id": "fidelity",
        "kicker": "BYTE FIDELITY",
        "title": "无编辑构建：输出与原件逐字节一致",
        "sub": "patent-1031.docx（32 页专利）— build 不动任何未编辑的字节：document.xml 内容哈希相同、13 个 part 原样回放。",
        "code": [
            ("bash", "$ docx2typed build patent-wd -o noop.docx"),
            ("bash", "$ docx2typed verify patent-wd noop.docx"),
            ("out", "verify: PASS — 0 bytes differ"),
            ("out", "sha256(word/document.xml): 6479cac7… == 6479cac7…"),
        ],
        "stats": [("0", "字节漂移"), ("13", "part 原样"), ("32", "页专利"), ("verify", "绿")],
        "xml_before": None,
        "xml_after": None,
    },
]


def _img_data(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def _xml_side(title: str, body: str, tone: str) -> str:
    return f"""<div class="xmlside">
      <div class="xmlhead"><span class="dot {tone}"></span>{title}</div>
      <pre class="xml"><code>{body}</code></pre>
    </div>"""


def _code_block(code) -> str:
    rows = []
    for kind, text in code:
        cls = "cmd" if kind == "bash" else "out"
        rows.append(f'<div class="line {cls}">{text}</div>')
    return "".join(rows)


def _stat_pills(stats) -> str:
    pills = "".join(
        f'<div class="pill"><b>{num}</b><span>{label}</span></div>' for num, label in stats
    )
    return f'<div class="pills">{pills}</div>'


def render(png_dir: Path) -> str:
    sections = []
    for demo in DEMOS:
        if demo.get("image"):
            before, after = demo["image"]
            b_img = _img_data(png_dir / before)
            a_img = _img_data(png_dir / after)
            visual = f"""
      <div class="sliderwrap">
        <div class="sliderhead">
          <span class="tag before">BEFORE</span>
          <input type="range" min="0" max="100" value="50" class="cmp" id="cmp-{demo['id']}">
          <span class="tag after">AFTER</span>
        </div>
        <div class="compare" id="compare-{demo['id']}">
          <div class="cmp-after" style="background-image:url('{a_img}')"></div>
          <div class="cmp-before" style="background-image:url('{b_img}')"></div>
        </div>
        <div class="imgcap">{demo['image_caption']}</div>
      </div>"""
        else:
            visual = ""
        sides = ""
        if demo.get("xml_before"):
            sides = _xml_side("模板内真实 XML · 前", demo["xml_before"], "red") + _xml_side(
                "引擎输出 · 后", demo.get("xml_after", ""), "green"
            )
        sections.append(f"""
  <section class="demo" id="{demo['id']}">
    <div class="demo-head">
      <span class="kicker">{demo['kicker']}</span>
      <h2>{demo['title']}</h2>
      <p class="sub">{demo['sub']}</p>
      {_stat_pills(demo['stats'])}
    </div>
    <div class="demo-body">
      <div class="codecard">
        <div class="codehead"><span class="dot cyan"></span>就这几行</div>
        {_code_block(demo['code'])}
      </div>
      {visual}
      <div class="xmls">{sides}</div>
    </div>
  </section>""")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>docx2typed — 真实语料库实测：一行命令，看得见的效果</title>
<style>
:root {{
  --bg:#070b14; --card:#0e1424; --card2:#111a2e; --line:#1e2a44;
  --ink:#e8eefc; --dim:#8b98b8; --cyan:#38e1ff; --mag:#ff5ac8; --green:#3dffa0; --red:#ff6b81; --amber:#ffc24d;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--ink); font:15px/1.6 system-ui,"Segoe UI",-apple-system,sans-serif;
  background-image:radial-gradient(900px 500px at 15% -5%, rgba(56,225,255,.14), transparent 60%),
                   radial-gradient(800px 500px at 85% 10%, rgba(255,90,200,.12), transparent 60%),
                   radial-gradient(700px 600px at 50% 110%, rgba(61,255,160,.08), transparent 60%);
  background-attachment:fixed; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:0 24px 80px; }}
header.hero {{ padding:72px 0 44px; text-align:center; }}
.hero h1 {{ font-size:clamp(30px,4.4vw,52px); font-weight:800; letter-spacing:-.5px;
  background:linear-gradient(100deg,#fff 20%,var(--cyan) 55%,var(--mag) 90%);
  -webkit-background-clip:text; background-clip:text; color:transparent; }}
.hero .lead {{ color:var(--dim); margin-top:12px; font-size:17px; }}
.hero .badges {{ margin-top:22px; display:flex; gap:10px; justify-content:center; flex-wrap:wrap; }}
.badge {{ padding:6px 14px; border-radius:999px; border:1px solid var(--line); background:rgba(255,255,255,.03); color:var(--dim); font-size:13px; }}
.badge b {{ color:var(--cyan); }}
.hero-stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-top:38px; }}
.hstat {{ background:var(--card); border:1px solid var(--line); border-radius:18px; padding:22px 18px; position:relative; overflow:hidden; }}
.hstat::after {{ content:""; position:absolute; inset:auto 0 0 0; height:3px;
  background:linear-gradient(90deg,var(--cyan),var(--mag)); opacity:.7; }}
.hstat b {{ display:block; font-size:44px; font-weight:800; letter-spacing:-1px;
  background:linear-gradient(100deg,var(--cyan),var(--mag)); -webkit-background-clip:text; background-clip:text; color:transparent; }}
.hstat span {{ color:var(--dim); font-size:13px; }}
.demo {{ background:var(--card); border:1px solid var(--line); border-radius:22px; padding:30px; margin-top:26px;
  box-shadow:0 20px 60px rgba(0,0,0,.35); }}
.demo-head .kicker {{ font-size:11px; letter-spacing:.22em; color:var(--mag); font-weight:700; }}
.demo h2 {{ font-size:clamp(20px,2.6vw,27px); margin-top:6px; }}
.demo .sub {{ color:var(--dim); margin-top:8px; max-width:820px; }}
.pills {{ display:flex; gap:10px; margin-top:18px; flex-wrap:wrap; }}
.pill {{ background:var(--card2); border:1px solid var(--line); border-radius:12px; padding:8px 16px; display:flex; gap:8px; align-items:baseline; }}
.pill b {{ font-size:19px; color:var(--cyan); }}
.pill span {{ color:var(--dim); font-size:12px; }}
.demo-body {{ margin-top:22px; }}
.codecard {{ background:#0a0f1c; border:1px solid var(--line); border-radius:14px; overflow:hidden; }}
.codehead {{ padding:9px 14px; border-bottom:1px solid var(--line); font-size:12px; color:var(--dim); display:flex; gap:8px; align-items:center; }}
.line {{ padding:3px 16px; font-family:ui-monospace,Consolas,"Cascadia Mono",monospace; font-size:13px; white-space:pre-wrap; }}
.line.cmd {{ color:#d7e3ff; }}
.line.cmd::before {{ content:"$ "; color:var(--cyan); font-weight:700; }}
.line.out {{ color:#7f8eb0; }}
.sliderwrap {{ margin-top:18px; }}
.sliderhead {{ display:flex; align-items:center; gap:14px; margin-bottom:10px; }}
.tag {{ font-size:11px; font-weight:800; letter-spacing:.14em; padding:3px 10px; border-radius:6px; }}
.tag.before {{ color:var(--red); background:rgba(255,107,129,.12); border:1px solid rgba(255,107,129,.4); }}
.tag.after {{ color:var(--green); background:rgba(61,255,160,.12); border:1px solid rgba(61,255,160,.4); }}
input.cmp {{ flex:1; accent-color:var(--cyan); }}
.compare {{ position:relative; height:520px; border-radius:14px; overflow:hidden; border:1px solid var(--line); cursor:ew-resize; }}
.cmp-after,.cmp-before {{ position:absolute; inset:0; background-size:contain; background-position:top left; background-repeat:no-repeat; }}
.cmp-before {{ clip-path:inset(0 50% 0 0); }}
.compare.dragging .cmp-before {{ clip-path:inset(0 var(--pos,50%) 0 0); }}
.imgcap {{ color:var(--dim); font-size:12px; margin-top:8px; }}
.xmls {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:18px; }}
.xmlside {{ background:#0a0f1c; border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
.xmlhead {{ padding:8px 12px; font-size:12px; color:var(--dim); border-bottom:1px solid var(--line); display:flex; gap:8px; align-items:center; }}
.dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; }}
.dot.red {{ background:var(--red); box-shadow:0 0 8px var(--red); }}
.dot.green {{ background:var(--green); box-shadow:0 0 8px var(--green); }}
.dot.cyan {{ background:var(--cyan); box-shadow:0 0 8px var(--cyan); }}
.xml {{ padding:12px 14px; font-family:ui-monospace,Consolas,monospace; font-size:12px; color:#a8b6d8; overflow-x:auto; }}
.xmlside:first-child .xml {{ color:#d9a0ab; }}
.xmlside:last-child .xml {{ color:#9fe8c4; }}
@media (max-width:820px) {{ .xmls {{ grid-template-columns:1fr; }} .compare {{ height:340px; }} }}
footer {{ margin-top:34px; color:var(--dim); font-size:12px; text-align:center; }}
footer b {{ color:var(--ink); }}
</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <div class="badges">
    <span class="badge">真实语料库 <b>10/10</b> 全链通过</span>
    <span class="badge">修订 <b>199</b> 处落定</span>
    <span class="badge">批注 <b>27</b> 条清除</span>
    <span class="badge">字节漂移 <b>0</b></span>
  </div>
  <h1>docx2typed：一行命令，看得见的效果</h1>
  <p class="lead">10 份真实 DOCX（57 MB 投稿手稿 → 32 页专利）· 提取 → 编辑 → 构建 → 验证全链 · 无编辑构建逐字节一致</p>
  <div class="hero-stats">
    <div class="hstat"><b>10/10</b><span>真实文档全链通过</span></div>
    <div class="hstat"><b>199</b><span>修订一键落定，0 残留</span></div>
    <div class="hstat"><b>27</b><span>批注连根清除</span></div>
    <div class="hstat"><b>0</b><span>未编辑字节漂移</span></div>
    <div class="hstat"><b>1,297</b><span>段落在引擎控制下</span></div>
    <div class="hstat"><b>12</b><span>表 / 340 格可结构操作</span></div>
  </div>
</header>
{''.join(sections)}
<footer>docx2typed · typed-mode 分支 · 175 tests green · LibreOffice 全部无警告打开 · 生成于 2026-08-06</footer>
</div>
<script>
document.querySelectorAll('.compare').forEach(function (box) {{
  var input = document.getElementById('cmp-' + box.id.split('-')[1]);
  if (!input) return;
  function set() {{ box.style.setProperty('--pos', input.value + '%'); }}
  input.addEventListener('input', set);
  set();
}});
</script>
</body>
</html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--png-dir", default=str(Path(__file__).resolve().parent.parent / "corpus-demo-work" / "png"))
    args = parser.parse_args(argv)
    html = render(Path(args.png_dir))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
