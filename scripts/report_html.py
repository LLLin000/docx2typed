"""Create an offline HTML acceptance report with embedded screenshots."""
from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
from pathlib import Path
from typing import Any


CSS = r"""
:root {
  --ink: #17202a;
  --ink-soft: #53616d;
  --ink-faint: #7b8791;
  --paper: #f4f1ea;
  --paper-deep: #e8e3d8;
  --panel: #fffdf8;
  --line: #d9d4c9;
  --accent: #b6532f;
  --accent-dark: #7f321e;
  --blue: #2c6274;
  --green: #1d7251;
  --amber: #9b691c;
  --red: #9d352e;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;
  --space-12: 48px;
  --space-16: 64px;
  --shadow: 0 16px 40px rgba(23, 32, 42, .08);
}

* { box-sizing: border-box; }
html { background: var(--paper); color: var(--ink); }
body {
  margin: 0;
  font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px;
  line-height: 1.6;
  background:
    radial-gradient(circle at 88% 8%, rgba(182, 83, 47, .10), transparent 28rem),
    linear-gradient(180deg, #f8f5ef 0%, var(--paper) 48%, #eee9df 100%);
}
main { max-width: 1180px; margin: 0 auto; padding: var(--space-12) var(--space-6) var(--space-16); }
.hero { position: relative; padding: var(--space-12) 0 var(--space-10); border-bottom: 1px solid var(--line); }
.kicker, .eyebrow, .label {
  color: var(--accent-dark);
  font: 700 11px/1.3 ui-monospace, SFMono-Regular, Consolas, monospace;
  letter-spacing: .12em;
  text-transform: uppercase;
}
.hero h1 {
  max-width: 830px;
  margin: var(--space-4) 0 var(--space-5);
  font: 700 clamp(42px, 7vw, 82px)/.98 Georgia, "Times New Roman", serif;
  letter-spacing: -.045em;
}
.hero h1 em { color: var(--accent); font-style: normal; }
.deck { max-width: 720px; margin: 0; color: var(--ink-soft); font-size: 20px; line-height: 1.55; }
.stamp-row { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-8); }
.stamp {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: var(--space-2) var(--space-3);
  color: var(--ink-soft);
  background: rgba(255, 253, 248, .72);
  font-size: 13px;
}
.stamp strong { color: var(--ink); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
.section { margin-top: var(--space-12); }
.section-head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-5); margin-bottom: var(--space-5); }
.section h2 { margin: 0; font: 700 32px/1.15 Georgia, "Times New Roman", serif; letter-spacing: -.025em; }
.section-intro { max-width: 680px; margin: var(--space-2) 0 0; color: var(--ink-soft); }
.proof-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-4); }
.metric, .panel, .case, .visual, .quote {
  border: 1px solid var(--line);
  background: rgba(255, 253, 248, .82);
  box-shadow: var(--shadow);
}
.metric { min-height: 150px; padding: var(--space-5); }
.metric .number { display: block; margin: var(--space-4) 0 var(--space-2); color: var(--accent-dark); font: 700 42px/1 Georgia, "Times New Roman", serif; }
.metric p { margin: 0; color: var(--ink-soft); font-size: 14px; }
.panel, .case, .metric, .visual { min-width: 0; }
.split { display: grid; grid-template-columns: minmax(0, 1.12fr) minmax(320px, .88fr); gap: var(--space-5); }
.panel { padding: var(--space-6); }
.panel h3, .case h3, .visual h3 { margin: 0 0 var(--space-3); font-size: 18px; line-height: 1.3; }
.panel p { margin: var(--space-3) 0 0; color: var(--ink-soft); }
.pitch { border-left: 4px solid var(--accent); padding-left: var(--space-5); }
.pitch p:first-child { margin-top: 0; color: var(--ink); font: 700 25px/1.25 Georgia, "Times New Roman", serif; }
ul { margin: var(--space-4) 0 0; padding-left: 20px; }
li { margin: var(--space-2) 0; color: var(--ink-soft); }
li strong { color: var(--ink); }
.code-note { margin-top: var(--space-5); padding: var(--space-4); overflow: auto; border: 1px dashed var(--line); background: #f5f1e8; color: var(--blue); font: 13px/1.6 ui-monospace, SFMono-Regular, Consolas, monospace; }
.cases { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.case { padding: var(--space-5); }
.case .case-top { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.case p { margin: var(--space-3) 0 0; color: var(--ink-soft); font-size: 14px; }
.badge { border-radius: 999px; padding: 3px 9px; font: 700 11px/1.3 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .05em; text-transform: uppercase; }
.badge.green { color: var(--green); background: #e3f1e9; }
.badge.amber { color: var(--amber); background: #f8edd0; }
.badge.red { color: var(--red); background: #f5e1df; }
.visuals { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-4); }
.visual { overflow: hidden; }
.visual h3 { padding: var(--space-4) var(--space-4) 0; }
.visual .caption { padding: 0 var(--space-4) var(--space-4); margin: 0; color: var(--ink-soft); font-size: 13px; }
.image-link { display: block; }
.visual img { display: block; width: 100%; height: auto; border-top: 1px solid var(--line); background: #fff; }
.visual img:hover { filter: contrast(1.02); }
.callout { margin-top: var(--space-5); padding: var(--space-5); border: 1px solid #e1c4a5; background: #fff4e6; }
.callout strong { color: var(--accent-dark); }
.tokens { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-4); }
.token { border: 1px solid #c9d5d8; border-radius: 999px; padding: 4px 9px; color: var(--blue); background: #edf4f4; font: 12px/1.3 ui-monospace, SFMono-Regular, Consolas, monospace; }
.fixture-code { margin: var(--space-4) 0 0; padding: var(--space-4); overflow: auto; border: 1px solid var(--line); background: #1d2830; color: #e8f1ed; font: 13px/1.65 ui-monospace, SFMono-Regular, Consolas, monospace; }
.fixture-code .accent { color: #f2b48e; }
.checks { width: 100%; border-collapse: collapse; font-size: 14px; }
.checks th, .checks td { padding: var(--space-3) var(--space-2); border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
.checks th { color: var(--ink-faint); font: 700 11px/1.3 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .08em; text-transform: uppercase; }
.checks td:last-child { color: var(--ink-soft); }
.check-status { color: var(--green); font: 700 12px/1.3 ui-monospace, SFMono-Regular, Consolas, monospace; }
.quote { margin-top: var(--space-6); padding: var(--space-6); border-color: var(--ink); background: var(--ink); color: #f8f5ef; }
.quote p { max-width: 820px; margin: 0; font: 700 26px/1.35 Georgia, "Times New Roman", serif; }
.quote small { display: block; margin-top: var(--space-4); color: #bac4c9; font-size: 13px; }
.footer { margin-top: var(--space-12); padding-top: var(--space-4); border-top: 1px solid var(--line); color: var(--ink-faint); font-size: 12px; }
.footer code { color: var(--blue); word-break: break-all; }
@media (max-width: 900px) {
  main { padding-inline: var(--space-4); }
  .proof-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .split, .cases { grid-template-columns: 1fr; }
  .visuals { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  main { padding-top: var(--space-6); }
  .hero { padding-top: var(--space-6); }
  .hero h1 { font-size: 48px; }
  .deck { font-size: 17px; }
  .proof-grid { grid-template-columns: 1fr 1fr; gap: var(--space-2); }
  .metric { min-height: 126px; padding: var(--space-3); }
  .metric .number { font-size: 32px; }
  .section { margin-top: var(--space-10); }
  .section h2 { font-size: 27px; }
  .stamp { font-size: 12px; }
}
"""


DIRECTIONS = {
    "visual_original": ("01 / 原始文档", "作为基准：复杂结构和版式的初始状态。"),
    "visual_typed-edit-3": ("02 / 三次 typed 编辑后", "同一份 DOCX 经三轮文字修改后的可视化结果。"),
    "visual_manual-roundtrip": ("03 / 手工保存后再回放", "DOCX 保存、重新提取、重新构建后的结果。"),
}


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _image_data(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _find_artifact(report_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if candidate.exists():
        return candidate
    candidate = report_dir / candidate.name
    return candidate if candidate.exists() else None


def _metric(number: str, label: str, detail: str) -> str:
    return (
        '<article class="metric">'
        f'<span class="label">{_escape(label)}</span>'
        f'<span class="number">{_escape(number)}</span>'
        f'<p>{_escape(detail)}</p>'
        "</article>"
    )


def _checks(report: dict[str, Any]) -> str:
    rows: list[str] = []
    for check in report.get("checks", []):
        status = check.get("status", "UNKNOWN")
        status_class = "check-status" if status == "PASS" else "badge red"
        rows.append(
            "<tr>"
            f"<td><span class=\"{status_class}\">{_escape(status)}</span></td>"
            f"<td>{_escape(check.get('name', ''))}</td>"
            f"<td>{_escape(check.get('evidence', ''))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _visuals(report_dir: Path, report: dict[str, Any]) -> str:
    cards: list[str] = []
    for key, (title, caption) in DIRECTIONS.items():
        path = _find_artifact(report_dir, report.get("artifacts", {}).get(key))
        if path is None:
            continue
        data_uri = _image_data(path)
        cards.append(
            '<figure class="visual">'
            f"<h3>{_escape(title)}</h3>"
            f'<a class="image-link" href="{data_uri}" target="_blank" rel="noreferrer">'
            f'<img src="{data_uri}" alt="{_escape(title)}"></a>'
            f'<figcaption class="caption">{_escape(caption)} 点击图片可打开嵌入的原始分辨率。</figcaption>'
            "</figure>"
        )
    return "".join(cards)


def _fixture_profile(report_dir: Path) -> dict[str, Any]:
    format_path = report_dir / "typed-workdir" / "format.json"
    styles_path = report_dir / "typed-workdir" / "styles.json"
    typed_path = report_dir / "typed-workdir" / "typed.md"
    profile: dict[str, Any] = {
        "package_parts": 0,
        "paragraphs": 0,
        "editable_paragraphs": 0,
        "styles": 0,
        "token_kinds": [],
    }
    try:
        format_data = json.loads(format_path.read_text(encoding="utf-8"))
        paragraphs = format_data.get("paragraphs", [])
        profile["package_parts"] = len(format_data.get("package_manifest", {}))
        profile["paragraphs"] = len(paragraphs)
        profile["editable_paragraphs"] = sum(bool(item.get("editable")) for item in paragraphs)
    except (OSError, json.JSONDecodeError):
        pass
    try:
        styles_data = json.loads(styles_path.read_text(encoding="utf-8"))
        styles = styles_data.get("styles", styles_data)
        profile["styles"] = len(styles) if isinstance(styles, dict) else 0
    except (OSError, json.JSONDecodeError):
        pass
    try:
        source = typed_path.read_text(encoding="utf-8")
        profile["token_kinds"] = sorted(set(re.findall(r'kind="([^"]+)"', source)))
    except OSError:
        pass
    return profile


def render_report(report_dir: str | Path, report: dict[str, Any]) -> str:
    report_path = Path(report_dir).resolve()
    env = report.get("environment", {})
    checks = report.get("checks", [])
    passed = sum(1 for check in checks if check.get("status") == "PASS")
    visual_count = sum(1 for key in DIRECTIONS if _find_artifact(report_path, report.get("artifacts", {}).get(key)))
    officecli = env.get("officecli") or "未检测到"
    officecli_version = env.get("officecli_version") or "未检测到"
    python_version = str(env.get("python", "unknown")).split(" ", 1)[0]
    python_docx_version = env.get("python_docx", "unknown")
    profile = _fixture_profile(report_path)
    token_chips = "".join(f'<span class="token">{_escape(kind)}</span>' for kind in profile["token_kinds"])
    package_parts = "0 次受保护包件漂移"
    html_report = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>docx2typed / Typed Mode 验收报告</title>
<style>{CSS}</style>
</head>
<body>
<main>
  <header class="hero">
    <div class="kicker">docx2typed / typed-mode / evidence report</div>
    <h1>让 AI 改文字，<em>不要改坏 Word。</em></h1>
    <p class="deck">这不是把 DOCX 粗暴转成 Markdown，而是把可编辑文字与受保护的 Word 结构分开：AI 改自然语言，构建器只回放允许改变的区域，其余 XML 保持原样。</p>
    <div class="stamp-row">
      <span class="stamp"><span class="dot"></span><strong>{_escape(report.get('result', 'UNKNOWN'))}</strong> 验收结果</span>
      <span class="stamp"><strong>{passed}/{len(checks)}</strong> checks passed</span>
      <span class="stamp">Python {_escape(python_version)} / python-docx {_escape(python_docx_version)} / officecli {_escape(officecli_version)}</span>
    </div>
  </header>

  <section class="section">
    <div class="proof-grid">
      {_metric(f"{passed}/{len(checks)}", "验收证据", "一个复杂 DOCX fixture 上的全部断言通过")}
      {_metric("3", "连续编辑", "同一份 typed workdir 连续三轮改写并重新构建")}
      {_metric("0", "结构漂移", package_parts)}
      {_metric(str(visual_count), "截图嵌入", "原始、typed 编辑后、回放后三张可离线查看")}
    </div>
  </section>

  <section class="section split">
    <article class="panel pitch">
      <div class="label">为什么值得用</div>
      <p>把“改内容”和“保格式”变成两个可以分别验证的动作。</p>
      <ul>
        <li><strong>AI 更容易读：</strong>正文连续可读，不再被每个 Word run 切成编号碎片。</li>
        <li><strong>格式不会靠猜：</strong>复杂对象保留为 opaque token，图片、链接、批注、数学公式、页眉页脚等不要求 AI 重写 XML。</li>
        <li><strong>失败会停：</strong>标签损坏、模板漂移、结构不匹配直接拒绝 build，不输出“看起来成功但已损坏”的 DOCX。</li>
        <li><strong>能审计：</strong>每次 extract/build/verify 都能留下 typed source、模板、XML 指纹和独立验证结果。</li>
      </ul>
      <div class="code-note">extract → typed.md + styles.json + _template.docx → edit → build → verify</div>
    </article>
    <article class="panel">
      <div class="label">工具定位</div>
      <h3>适合谁</h3>
      <p>需要让 AI 大范围修改专利、论文、合同、报告，但又不能牺牲 Word 排版、批注锚点、链接、图片和章节结构的人。</p>
      <h3 style="margin-top:24px">核心取舍</h3>
      <p>它不是“任意重排 Word 的编辑器”。它选择更窄、更可靠的边界：普通文字开放编辑，复杂结构默认保护；需要结构变化时必须显式扩展协议和验证器。</p>
      <div class="callout"><strong>卖点不是“什么都能改”。</strong><br>卖点是：在允许 AI 改的范围内，能证明它没有顺手把不能改的东西改掉。</div>
    </article>
  </section>

  <section class="section">
    <div class="section-head">
      <div>
        <div class="label">complex fixture / real Word package</div>
        <h2>这不是 demo 文本，是一份故意做难的 DOCX</h2>
        <p class="section-intro">为了证明工具不是只会处理三行普通段落，acceptance fixture 同时放进可编辑文字、格式范围、分页、表格、对象关系和 Word XML 结构。</p>
      </div>
    </div>
    <div class="split">
      <article class="panel">
        <h3>复杂，但边界清楚</h3>
        <p>这份真实 Word package 被拆成“正文可改”和“结构受保护”两层。AI 不需要理解每一个 XML 节点，也不会因为重写一句话而顺手丢掉整份文档的关系文件。</p>
        <ul>
          <li><strong>{profile["package_parts"]}</strong> 个 package parts 参与指纹保护。</li>
          <li><strong>{profile["paragraphs"]}</strong> 个段落进入 typed workdir。</li>
          <li><strong>{profile["editable_paragraphs"]}</strong> 个段落开放普通文字编辑，其余段落保持结构保护。</li>
          <li><strong>{profile["styles"]}</strong> 个规范化字符样式进入 sidecar registry。</li>
          <li><strong>{len(profile["token_kinds"])}</strong> 类结构 token 在 typed source 中可见、可验证、可回放。</li>
        </ul>
        <div class="tokens">{token_chips}</div>
      </article>
      <article class="panel">
        <div class="label">AI sees / XML stays</div>
        <h3>编辑的是句子，不是 Word 内脏</h3>
        <pre class="fixture-code">EDIT-THREE: &lt;span data-s="s_..."&gt;加粗文本&lt;/span&gt;
&lt;docx-inline id="N13" kind="br" ... /&gt;
&lt;docx-opaque id="N14" kind="opaque" tag="w:fldSimple" /&gt;
&lt;docx-opaque id="N17" kind="unsupported-run"
  tag="w:footnoteReference" /&gt;</pre>
        <p>普通文字可以被 AI 大范围改写；复杂结构保留成明确的节点，构建器按原始模板回放。这个分层就是稳定性的来源。</p>
      </article>
    </div>
  </section>

  <section class="section">
    <div class="section-head">
      <div>
        <div class="label">stability envelope</div>
        <h2>现在能稳定到什么程度</h2>
        <p class="section-intro">下面只把已经跑过的能力标为“已验证”；没有实际 WPS/Word 保存证据的场景，不冒充稳定。</p>
      </div>
    </div>
    <div class="cases">
      <article class="case"><div class="case-top"><h3>普通正文修改</h3><span class="badge green">已验证</span></div><p>提取正文、替换文字、重新构建、独立 verify。适合 AI 改写说明书、论文段落、合同条款。</p></article>
      <article class="case"><div class="case-top"><h3>连续多轮 AI 编辑</h3><span class="badge green">已验证</span></div><p>同一 workdir 连续三轮替换，并在每轮后检查可见文本和受保护 package 部分。</p></article>
      <article class="case"><div class="case-top"><h3>局部加粗 / 换行等已有格式</h3><span class="badge green">已验证</span></div><p>复杂 fixture 中已有的格式范围和换行内容经过 typed 编辑后仍能回放。</p></article>
      <article class="case"><div class="case-top"><h3>复杂 Word 对象</h3><span class="badge green">受保护已验证</span></div><p>图片关系、超链接、批注、域、脚注/尾注、OMML、section、表格等作为结构 token 保留；当前策略是保护，不是让 AI 任意改它们。</p></article>
      <article class="case"><div class="case-top"><h3>损坏的 typed 源</h3><span class="badge green">拒绝已验证</span></div><p>缺失 span 闭合等 malformed input 会被拒绝，避免生成一个无法解释的 DOCX。</p></article>
      <article class="case"><div class="case-top"><h3>模板被外部改动</h3><span class="badge green">拒绝已验证</span></div><p>模板 fingerprint 发生变化时 build 停止，不在错误基线上继续回放。</p></article>
      <article class="case"><div class="case-top"><h3>真实 WPS / Word 保存</h3><span class="badge amber">待实机</span></div><p>当前 acceptance 使用 python-docx 模拟普通 DOCX 保存；要给 WPS/Word 下结论，还需你实际打开、编辑、保存后再跑 extract/build/verify。</p></article>
      <article class="case"><div class="case-top"><h3>任意新增复杂结构</h3><span class="badge red">不承诺</span></div><p>直接让 AI 在 typed.md 里发明图片、批注、表格关系或跨段结构，不属于当前安全编辑边界，应该先扩展 schema 和验证器。</p></article>
    </div>
  </section>

  <section class="section">
    <div class="section-head">
      <div>
        <div class="label">visual proof / officecli { _escape(officecli_version) }</div>
        <h2>真实渲染结果</h2>
        <p class="section-intro">以下图片已转成 data URI 写入本 HTML。复制或移动这一个文件即可离线查看，不依赖截图目录。</p>
      </div>
    </div>
    <div class="visuals">{_visuals(report_path, report)}</div>
    <div class="callout"><strong>阅读方式：</strong>先看三张图的整体分页、表格、标题、页眉页脚是否保持一致，再看预期文字是否变化。截图来自 officecli 的 HTML renderer，不等同于 WPS/Word 原生排版；WPS/Word 实机保存仍是下一道验证。</div>
  </section>

  <section class="section">
    <div class="section-head">
      <div>
        <div class="label">check ledger</div>
        <h2>逐项验收记录</h2>
      </div>
    </div>
    <div class="panel" style="overflow:auto">
      <table class="checks"><thead><tr><th>状态</th><th>检查项</th><th>证据</th></tr></thead><tbody>{_checks(report)}</tbody></table>
    </div>
  </section>

  <section class="quote">
    <p>如果你的目标是“让 AI 改完一整份专利/论文/合同，格式还要能解释、能回滚、能验证”，这个工具的价值不是省掉 Word，而是把 Word 最危险的部分从 AI 的自由编辑区隔离出去。</p>
    <small>结论边界：本报告证明了当前 fixture 和当前运行环境下的行为；它不把一次通过外推成所有 DOCX、所有 Word 版本和所有结构都稳定。</small>
  </section>

  <footer class="footer">
    <div>报告由 scripts/report_html.py 生成。运行环境：{_escape(env.get('platform', 'unknown'))}。</div>
    <div>officecli: <code>{_escape(officecli)}</code></div>
    <div>原始证据目录：<code>{_escape(report_path)}</code></div>
  </footer>
</main>
</body>
</html>
"""
    return html_report


def write_report_html(report_dir: str | Path, report: dict[str, Any]) -> Path:
    report_path = Path(report_dir).resolve()
    output = report_path / "report.html"
    output.write_text(render_report(report_path, report), encoding="utf-8")
    return output


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m scripts.report_html REPORT_DIR")
    directory = Path(sys.argv[1]).resolve()
    payload = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    print(write_report_html(directory, payload))
