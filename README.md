# docx2typed

> **Structure-preserving DOCX text editing for agents and reviewers.**
>
> `docx2typed` lets an agent edit the words in a `.docx` without flattening the document into a lossy text or HTML approximation. The typed workdir locks style ownership, anchors, tracked-revision identity, comments, table structure, content controls, and untouched package parts; only explicitly requested text moves.

<p align="center">
  <img src="docs/assets/review-console-revisions.png" alt="docx2typed review console showing tracked revisions and a fixed review index" width="100%" style="max-width:100%;height:auto;display:block">
</p>

<p align="center"><sub>合成审阅演示：行内修订、固定跳转索引，以及明确的 build + verify 交付边界；页面内容不依赖真实用户文档。</sub></p>

## Why this is different / 为什么它不只是 DOCX 转 HTML

| What users see | What the engine guarantees |
|---|---|
| A continuous document surface with a fixed review index. | Review is rendered from the canonical typed AST, not the lossy `edit.md` projection. |
| Text edits, tracked revisions, comment instructions, table operations, and content controls. | Unchanged structure stays locked; ambiguous style ownership fails closed instead of being guessed. |
| A new DOCX plus an evidence trail. | `build` validates before writing, `verify` independently re-derives the result, and LibreOffice checks interoperability. |

### The release bar

The release qualification suite is deliberately adversarial: **32 deterministic black-box tasks**, **6 metamorphic relations**, and a hard gate of **0 unknown capabilities** and **0 silent corruption**. The project is not “done” because a browser page looks plausible; the output DOCX must pass the structural and package-level gates.

> **Positioning:** this is a structure-preserving editing engine, not a browser clone of Microsoft Word. The browser console is a semantic review surface; `build` + `verify` + LibreOffice interop are the delivery proof.

## Core contract / 核心契约

| Contract | Guarantee |
|---|---|
| Only requested text moves. | Untouched paragraphs, styles, anchors, and package parts are replayed without side effects. |
| Source files stay safe. | `extract` never mutates the input; structural operations and wholesale revision decisions write a new DOCX/workdir. |
| Style ownership stays explicit. | Edits are planned against `regions.md`; mixed-region rewrites are rejected rather than guessed. |
| Comments stay by default. | Agents may act on comment instructions, but comment IDs, text, dates, authors, and anchors remain unless the user explicitly requests deletion. |

## 安装 / Install

### 环境 / Requirements

- Python **3.11+**
- `python-docx` and `mcp` are installed from the package metadata
- LibreOffice Writer is recommended for the final interoperability check

```bash
python -m pip install -e .

# Confirm the installed CLI.
docx2typed extract --help
```

The package exposes these entry points after installation:

```text
docx2typed          # CLI

docx2typed-mcp      # stdio MCP server
docx2typed-review   # localhost review server
```

When running directly from a source checkout, replace the installed module name with `scripts`:

```bash
python -m scripts extract input.docx -o workdir
python -m scripts.review_console workdir -o review.html
python -m scripts.review_server workdir --port 8876
```

## 五分钟流程 / Five-minute workflow

### 1. Extract / 提取

```bash
docx2typed extract input.docx -o workdir
```

`input.docx` remains unchanged. The workdir records the source/template fingerprints and creates the canonical typed projection plus editable sidecars.

### 2. Read once, then inspect locally / 先读全文，再局部检查

```bash
docx2typed view workdir --mode clean
docx2typed view workdir --mode style
docx2typed view workdir --mode raw

# Freshness before editing.
docx2typed edit status workdir
```

- `clean`: continuous prose for understanding the document.
- `style`: prose plus style-region diagnostics.
- `raw`: typed markers, paragraph IDs, tables, ranges, anchors, revisions, and structural tokens.

Read the full `clean` projection once. Use `regions.md` and paragraph-level inspection only for the passages you will change.

### 3. Edit / 修改

Open `workdir/edit.md` in an editor and change text inside the relevant style region. Do not rewrite the `typed.md` header or structural markers. Then synchronize the draft:

```bash
# Normal text edit: no new tracked revisions.
docx2typed edit sync workdir --no-track

# Or make the edit visible as real w:ins/w:del revisions.
docx2typed edit sync workdir --track --author "Reviewer"
```

For a raw `typed.md` change, refresh the projection first:

```bash
docx2typed edit refresh workdir
```

If the draft is intentionally discarded, use `--discard`; do not overwrite a dirty draft accidentally.

### 4. Build / 构建

```bash
docx2typed validate workdir
docx2typed build workdir -o output.docx
```

`build` requires a valid clean state and fails closed on invalid structure, stale edits, unresolved conflicts, or unsafe range changes.

### 5. Verify / 验证

```bash
docx2typed verify workdir output.docx
```

`verify` independently re-derives the output against the workdir. The structured evidence includes checks, revision counts/authors, and surviving comment IDs.

### 6. Interop / 互操作

Open or convert the built DOCX with LibreOffice Writer. A deliverable is complete only when `verify` passes and LibreOffice opens/converts it without repair prompts.


## 格式查看与审阅控制台 / Format inspection and review console

### 静态 HTML / Standalone HTML

Generate a self-contained review page from the canonical typed AST:

```bash
# Installed package:
python -m docx2typed.review_console long-wd -o review.html

# Source checkout:
python -m scripts.review_console long-wd -o review.html
```

Open `review.html` in a browser. The console renders `typed.md` together with `styles.json`; it does not use the lossy `edit.md` projection or an external DOCX-to-HTML renderer.

### Local interactive server / 本地交互服务

```bash
docx2typed-review long-wd --host 127.0.0.1 --port 8876
```

Open <http://127.0.0.1:8876/>. The server provides the review page and the local handoff API. The review index is sticky: selecting a revision or comment jumps the document surface to its corresponding paragraph while keeping the decision context visible. Technical style diagnostics stay opt-in so ordinary readers see the document, not internal metadata.

For a source checkout:

```bash
python -m scripts.review_server long-wd --host 127.0.0.1 --port 8876
```

The browser surface is deliberately semantic and reader-first:

- paragraph flow remains continuous instead of becoming isolated evidence cards;
- tracked insertions/deletions retain their inline location and metadata;
- comment records show real text and anchor paragraphs;
- style ownership comes from the Word style registry and `styles.json`;
- unsupported Word layout details are not silently rewritten; final fidelity is checked in the built DOCX.

### Screenshot / 截图

<table>
  <tr>
    <td width="72%"><img src="docs/assets/review-console-desktop.png" alt="Desktop docx2typed review console showing a continuous document surface and fixed review index" style="max-width:100%;height:auto;display:block"></td>
    <td width="28%"><img src="docs/assets/review-console-mobile.png" alt="Mobile docx2typed review console with compact controls and no horizontal overflow" style="max-width:100%;height:auto;display:block"></td>
  </tr>
  <tr>
    <td><sub><strong>Desktop</strong> — paper-like reading stage; the review index stays visible while the document remains continuous.</sub></td>
    <td><sub><strong>Mobile</strong> — the rail collapses into an accessible compact control without horizontal overflow.</sub></td>
  </tr>
</table>

## 常用工作流 / Common workflows

### 普通文本编辑 / Clean text edit

```bash
docx2typed extract input.docx -o workdir
docx2typed view workdir --mode clean
# edit workdir/edit.md within one style region
docx2typed edit sync workdir --no-track
docx2typed build workdir -o edited.docx
docx2typed verify workdir edited.docx
```

### 修订式编辑 / Tracked edit

```bash
docx2typed extract input.docx -o tracked-wd
# edit tracked-wd/edit.md
docx2typed edit sync tracked-wd --track --author "AI Reviewer"
docx2typed build tracked-wd -o tracked.docx
docx2typed verify tracked-wd tracked.docx
```

The output contains real `w:ins`/`w:del` nodes. Existing revisions are left untouched; new revisions receive the session author/date.

### 单项修订决策 / Single revision decision

Read `revisions.json` first. A revision key has the form `part|kind|w:id|fingerprint`:

```bash
docx2typed decide accept "word/document.xml|insert|8|..." \
  --workdir tracked-wd --fingerprint "..."

docx2typed decide reject "word/document.xml|delete|7|..." \
  --workdir tracked-wd --fingerprint "..."
```

The fingerprint is a defensive check against stale review selections.

### 全量接受或拒绝 / Wholesale settlement

```bash
docx2typed decide accept-all \
  --workdir tracked-wd \
  --output accepted.docx \
  --workdir-out accepted-wd

docx2typed verify accepted-wd accepted.docx
```

`reject-all` has the same shape. The source workdir is never mutated; the new workdir is a clean baseline.

### 批注返修 / Comment review

Comments are first-class review objects in MCP:

```text
workdir_open(workdir, author="AI Reviewer", track=true)
list_comments()
get_comment(comment_id)
# make region-scoped tracked edits
commit_sync()
build_docx(output="comment-reviewed.docx")
verify_output(output="comment-reviewed.docx")
```

The normal agent path leaves every comment ID, author, date, text, and anchor in place. Do not call `delete_comment` just because the requested edit is complete. If the user explicitly asks to delete comment `1`, the CLI equivalent is:

```bash
docx2typed decide comment-delete 1 --workdir workdir
```

### 表格结构 / Table structure

Table references are body-level ordinals (`T0`, `T1`, ...), learned from `view --mode raw`:

```bash
docx2typed decide table-insert-row T0 \
  --workdir workdir --args "2" \
  --output table-row.docx --workdir-out table-row-wd

docx2typed decide table-merge-cells T0 \
  --workdir workdir --args "0 1 2" \
  --output table-merged.docx --workdir-out table-merged-wd
```

Rows/columns are 0-based. Merge is fail-closed if spanned cells contain text; use an explicit discard option only when content loss is intended. Table tools create a new DOCX/workdir and never rewrite cell text implicitly.

### Unicode 上下标归一化 / Unicode vertical normalization

```bash
docx2typed audit scan workdir -o scan.json
# A human reviews scan.json and creates a hash-bound approved policy.
docx2typed audit apply workdir \
  --scan scan.json \
  --policy policy.json \
  -o normalized.docx \
  --workdir-out normalized-wd

docx2typed verify normalized-wd normalized.docx
```

Classification is a suggestion. Ambiguous candidates are preserved until an approved policy says otherwise.

### 内容控件文本 / Content-control text

Content-control paragraphs are exposed as IDs such as `S0.P0`. Edit their text through `edit.md` or MCP exactly like body text. The control properties (`w:sdtPr`) stay locked; structural insertion/removal of the control itself is outside this contract.

## MCP 集成 / MCP integration

After `python -m pip install -e .`, configure any stdio MCP host with:

```json
{
  "mcpServers": {
    "docx2typed": {
      "command": "python",
      "args": ["-m", "docx2typed.mcp_server"]
    }
  }
}
```

If the host does not inherit the environment where the package is installed, add a `cwd` or use the absolute Python executable for that environment. The server and CLI use the same typed-workdir engine and the same build/verify gates.

Recommended MCP sequence:

```text
workdir_open
list_paragraphs / get_paragraph / list_comments
replace_text / batch_edit / insert_paragraph / delete_paragraph
diff_preview
commit_sync
build_docx
verify_output
```

MCP edits are region-scoped. Unchanged characters keep their exact style; replacements inherit the replaced region's style only when ownership is unambiguous; mixed-region rewrites are rejected instead of guessed.

## Workdir 产物 / Workdir artifacts

| 文件 | 用途 / Purpose |
|---|---|
| `typed.md` | Canonical typed AST projection; includes paragraph and structural markers. |
| `edit.md` | Human-readable editable draft; prose changes are synchronized back to the AST. |
| `format.json` | Paragraph/token/package-part structure and source metadata. |
| `styles.json` | Word `rPr`-derived style registry used by the semantic console. |
| `regions.md` | Style-region boundaries and indices for safe edit planning. |
| `revisions.json` / `revisions.md` | Tracked-revision inventory and revision keys. |
| `edit.state.json` / `edit.state.json.run.json` | Freshness state and hash-bound edit evidence. |
| `_template.docx` | Preserved package template used when rebuilding. |
| `.review/` | Local review drafts, snapshots, history, and collaboration state when the review server is used. |

## 验收与开发 / Verification and development

Run the focused suite with scratch space on a non-system drive when needed:

```bash
python -m pytest -q --basetemp=D:/L/AppData/pytest-tmp
```

Smoke commands:

```bash
python -m scripts.acceptance_corpus --workdir D:/L/AppData/docx2typed-corpus-run
python -m scripts.tool_smoke --workdir D:/L/AppData/smoke-run
```

Release qualification uses deterministic fixtures, 32 black-box tasks, 6 metamorphic relations, and agent prompts:

```bash
python -m scripts.release_fixtures --outdir corpus/release
python -m scripts.release_acceptance \
  --report reports/release-local \
  --workdir D:/L/AppData/release-run
python -m scripts.agent_bench --list
python -m scripts.agent_bench --grade <task> <out.docx> <workdir>
```

A green release summary requires:

```text
task acceptance N/N
metamorphic N/N
unknown capability 0
silent corruption 0
```

## 长文演示 / Longer article example

The following creates a deliberately longer, structured article instead of a one-line fixture. It includes headings, long prose, mixed emphasis, superscript/subscript, and a table so the review surface has real vertical length and format markers to render.

Save it as `make_long_article.py`, then run it from the same directory:

```python
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt


def add_body(document: Document, text: str) -> None:
    paragraph = document.add_paragraph(text)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.line_spacing = 1.25


doc = Document()
section = doc.sections[0]
section.top_margin = Pt(54)
section.bottom_margin = Pt(54)
section.left_margin = Pt(64)
section.right_margin = Pt(64)

heading = doc.add_heading("面向科研写作的可验证文档审阅工作流", level=0)
heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

subtitle = doc.add_paragraph()
run = subtitle.add_run("一份用于测试结构、格式、修订与批注边界的长篇示例")
run.italic = True

abstract = (
    "摘要：长篇技术文档往往同时包含正文、标题层级、字体变化、上下标、批注、修订、表格和分页标记。"
    "如果编辑器只把 DOCX 转成一段纯文本，读者虽然能看到句子，却无法判断一次改写是否越过了样式边界，"
    "也无法证明没有被选中的图表、批注锚点和包内 XML 是否仍然保持原状。本文把文档处理拆成提取、阅读、编辑、"
    "审阅、构建和验证六个连续阶段，并用可追踪的工作目录记录每一阶段的输入、输出和哈希证据。"
)
doc.add_heading("摘要", level=1)
add_body(doc, abstract)

doc.add_heading("一、问题背景", level=1)
add_body(
    doc,
    "科研人员处理实验报告、专利说明书、基金申请书和学位论文时，最常见的风险不是某个句子缺少一个形容词，"
    "而是修改文字之后悄悄改变了原文档的格式和结构。例如，标题可能从居中变成左对齐，变量的上标可能被普通字符替代，"
    "表格中的一个空单元格可能被错误地复制了相邻文本，批注范围也可能因为段落重建而失效。问题的难点在于，文本语义和"
    "WordprocessingML 结构同时存在：人需要连续可读的文章，工具则必须保留每个可验证的边界。"
)


doc.add_heading("二、可验证的中间表示", level=1)
add_body(
    doc,
    "typed workdir 的核心不是一个临时文本缓存，而是一份带有段落 ID、样式区域、基线哈希和来源部件信息的中间表示。"
    "clean projection 适合快速通读全文，style projection 显示字体、字号、颜色、上下标和其他格式归属，raw projection 则把"
    "段落标记、表格引用、修订节点、批注锚点和不可展开的结构节点都放回视野。编辑时，工具首先比较基线与草稿，再判断替换是否"
    "只覆盖一个样式区域；如果一次替换混合了多个区域，系统宁可停止并给出拆分建议，也不会猜测新的样式。"
)


doc.add_heading("三、修订与批注的职责边界", level=1)
paragraph = doc.add_paragraph()
paragraph.add_run("修订是可决策的变更，批注是外部审阅者的指示。 ").bold = True
paragraph.add_run(
    "在 track mode 中，一次替换会生成对应的删除和插入修订，作者与日期写入 Word 修订节点；在 comment review 中，"
    "工具读取批注文本和锚点，完成相应的正文编辑，但默认保留批注本身。这样，老师或用户仍可以在 Word 中看到原始指示，"
    "并自行决定何时清理它们。只有明确执行 comment-delete，批注条目、起止锚点和引用才会一起删除。"
)


doc.add_heading("四、格式标记与真实输出", level=1)
add_body(
    doc,
    "格式标记不应只在浏览器里看起来正确。粗体、斜体、删除线、字体、字号、颜色、底纹、上下标、下划线、段落对齐、"
    "分页标记、表格和内容控件都需要经过同一套结构化路径。浏览器审阅控制台负责让人快速定位段落和修订；最终 DOCX 则由"
    "build 重新组装，由 verify 独立检查，再通过 LibreOffice 做互操作检查。浏览器无法完整模拟 Word 排版时，结构不会被静默"
    "扁平化，交付判断仍以 DOCX 级别的证据为准。"
)


doc.add_heading("五、一个小型实验表", level=1)
table = doc.add_table(rows=1, cols=3)
table.style = "Table Grid"
for cell, value in zip(table.rows[0].cells, ["阶段", "输入", "可检查证据"]):
    cell.text = value
for row in [
    ("提取", "source.docx", "source/template fingerprint"),
    ("编辑", "edit.md", "region ownership + run evidence"),
    ("审阅", "revisions.json", "revision key + comment IDs"),
    ("交付", "output.docx", "verify + LibreOffice"),
]:
    cells = table.add_row().cells
    for cell, value in zip(cells, row):
        cell.text = value


doc.add_heading("六、结语", level=1)
add_body(
    doc,
    "可验证的文档编辑不是把 Word 文件转换得像文本，而是让每次文字变化都有明确的范围、状态、证据和回滚边界。"
    "当读取、编辑、审阅、构建和验证被串成一条连续链路时，格式保真不再依赖一次幸运的导入导出，也不再要求用户在每个"
    "段落之后手工检查整个文件。这个示例故意保留足够长的正文，使侧边栏跳转、连续滚动、样式诊断和最终交付检查都能在真实"
    "的页面长度上被观察。"
)

# Add explicit format markers to the final paragraph.
marker = doc.add_paragraph("标记探针：")
marker.add_run("bold").bold = True
marker.add_run(" / ")
marker.add_run("italic").italic = True
marker.add_run(" / ")
sup = marker.add_run("x2")
sup.font.superscript = True
marker.add_run(" / ")
sub = marker.add_run("H2O")
sub.font.subscript = True


doc.save("long_article.docx")
print("wrote long_article.docx")
```

Run the full flow:

```bash
python make_long_article.py

docx2typed extract long_article.docx -o long-wd
docx2typed view long-wd --mode clean
docx2typed view long-wd --mode style

# Edit long-wd/edit.md, then:
docx2typed edit sync long-wd --track --author "Reviewer"
docx2typed build long-wd -o long_article-reviewed.docx
docx2typed verify long-wd long_article-reviewed.docx
```

## 文档地图 / Documentation map

- [`SKILL.md`](SKILL.md) — agent-facing contract and branch table.
- [`capabilities.md`](capabilities.md) — every CLI atom and MCP tool with syntax and exit contracts.
- [`composites.md`](composites.md) — seven workflows and end-to-end playbooks.
- [`verification.md`](verification.md) — freshness, fail-closed, byte-fidelity, and interop gates.
- [`docs/rpr-reference.md`](docs/rpr-reference.md) — Word `rPr` XML to style translation notes.

## English quick reference

1. Install with `python -m pip install -e .`.
2. Extract without mutating the source: `docx2typed extract input.docx -o workdir`.
3. Read `view --mode clean` once; inspect `style` or `raw` only where needed.
4. Edit `edit.md` inside the regions listed by `regions.md`.
5. Synchronize with `edit sync --no-track` or `edit sync --track --author NAME`.
6. Build, independently verify, and run a LibreOffice interoperability check.
7. Use `docx2typed-review workdir --port 8876` for the fixed review index and paragraph jumps.
8. Use MCP for region-scoped agent edits, revision decisions, comment inventory, collaboration, table operations, build, and verification.

The safe default is conservative: comments stay, ambiguous style ownership fails closed, source workdirs are not mutated by structural operations, and no document is called finished until the output evidence is green.
