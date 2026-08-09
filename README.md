# docx2typed

[Chinese version](README.zh-CN.md)

[Agent installation](https://github.com/LLLin000/docx2typed-typed-mode/blob/main/Installation.md)

> Give the [agent installation workflow](https://github.com/LLLin000/docx2typed-typed-mode/blob/main/Installation.md)
> to an agent for automatic PyPI, MCP, and temporary Tailscale phone-collaboration setup.

> **Structure-preserving DOCX text editing for agents and reviewers.**
>
> `docx2typed` lets an agent edit the words in a `.docx` without flattening the document into a lossy text or HTML approximation. The typed workdir locks style ownership, anchors, tracked-revision identity, comments, table structure, content controls, and untouched package parts; only explicitly requested text moves.

<p align="center">
  <img src="docs/assets/review-console-revisions.png" alt="docx2typed review console showing tracked revisions and a fixed review index" width="100%" style="max-width:100%;height:auto;display:block">
</p>

<p align="center"><sub>Synthetic review demo: inline tracked revisions, a fixed paragraph-jump index, and an explicit build + verify handoff. No real user document data is used.</sub></p>

## Why this is different

| What users see | What the engine guarantees |
|---|---|
| A continuous document surface with a fixed review index. | Review is rendered from the canonical typed AST, not the lossy `edit.md` projection. |
| Text edits, tracked revisions, comment instructions, table operations, and content controls. | Unchanged structure stays locked; ambiguous style ownership fails closed instead of being guessed. |
| A new DOCX plus an evidence trail. | `build` validates before writing, `verify` independently re-derives the result, and LibreOffice checks interoperability. |

### The release bar

The release qualification suite is deliberately adversarial: **32 deterministic black-box tasks**, **6 metamorphic relations**, and a hard gate of **0 unknown capabilities** and **0 silent corruption**. The project is not “done” because a browser page looks plausible; the output DOCX must pass the structural and package-level gates.

> **Positioning:** this is a structure-preserving editing engine, not a browser clone of Microsoft Word. The browser console is a semantic review surface; `build` + `verify` + LibreOffice interoperability are the delivery proof.

## Core contract

| Contract | Guarantee |
|---|---|
| Only requested text moves. | Untouched paragraphs, styles, anchors, and package parts are replayed without side effects. |
| Source files stay safe. | `extract` never mutates the input; structural operations and wholesale revision decisions write a new DOCX/workdir. |
| Style ownership stays explicit. | Edits are planned against `regions.md`; mixed-region rewrites are rejected rather than guessed. |
| Comments stay by default. | Agents may act on comment instructions, but comment IDs, text, dates, authors, and anchors remain unless the user explicitly requests deletion. |

## Install

### Requirements

- Python **3.11+**
- `python-docx` and `mcp` are installed from the package metadata
- LibreOffice Writer is recommended for the final interoperability check
- Tailscale is optional and only needed for phone access

### One-command source install

From a source checkout, use the installer for your platform:

```powershell
# Windows PowerShell
.\install.ps1

# Development install: reflect source edits immediately.
.\install.ps1 -Editable
```

```bash
# macOS / Linux
./install.sh

# Development install: reflect source edits immediately.
./install.sh --editable
```

Both installers create `.venv`, install the current checkout without changing
the system Python, and smoke-check the `docx2typed` CLI.

### PyPI install

```bash
python -m pip install --upgrade docx2typed

# Or install an isolated CLI with uv.
uv tool install --upgrade docx2typed

# Confirm the installed CLI.
docx2typed extract --help
```

### Source checkout manual install

```bash
python -m pip install .

# Development checkout:
python -m pip install -e .
```

The package exposes these entry points after installation:

```text
docx2typed          # CLI, including the mcp and review subcommands
docx2typed-mcp      # stdio MCP server
docx2typed-review   # localhost review server
```

The unified commands are useful for one-line tool configuration:

```bash
docx2typed mcp
docx2typed review workdir --host 127.0.0.1 --port 8876
```

When running directly from a source checkout, replace the installed module name with `scripts`:

```bash
python -m scripts extract input.docx -o workdir
python -m scripts.review_console workdir -o review.html
python -m scripts.review_server workdir --port 8876
```

## Five-minute workflow

### 1. Extract

```bash
docx2typed extract input.docx -o workdir
```

`input.docx` remains unchanged. The workdir records the source/template fingerprints and creates the canonical typed projection plus editable sidecars.

### 2. Read once, then inspect locally

```bash
docx2typed view workdir --mode clean
docx2typed view workdir --mode style
docx2typed view workdir --mode raw

# Check freshness before editing.
docx2typed edit status workdir
```

- `clean`: continuous prose for understanding the document.
- `style`: prose plus style-region diagnostics.
- `raw`: typed markers, paragraph IDs, tables, ranges, anchors, revisions, and structural tokens.

Read the full `clean` projection once. Use `regions.md` and paragraph-level inspection only for the passages you will change.

### 3. Edit

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

### 4. Build

```bash
docx2typed validate workdir
docx2typed build workdir -o output.docx
```

`build` requires a valid clean state and fails closed on invalid structure, stale edits, unresolved conflicts, or unsafe range changes.

### 5. Verify

```bash
docx2typed verify workdir output.docx
```

`verify` independently re-derives the output against the workdir. The structured evidence includes checks, revision counts/authors, and surviving comment IDs.

### 6. Interoperate with Word-compatible tooling

Open or convert the built DOCX with LibreOffice Writer. A deliverable is complete only when `verify` passes and LibreOffice opens/converts it without repair prompts.

## Format inspection and review console

### Standalone HTML

Generate a self-contained review page from the canonical typed AST:

```bash
# Installed package:
python -m docx2typed.review_console long-wd -o review.html

# Source checkout:
python -m scripts.review_console long-wd -o review.html
```

Open `review.html` in a browser. The console renders `typed.md` together with `styles.json`; it does not use the lossy `edit.md` projection or an external DOCX-to-HTML renderer.

### Local interactive server

```bash
docx2typed-review long-wd --host 127.0.0.1 --port 8876
```

Open <http://127.0.0.1:8876/>. The server provides the review page and the local handoff API. The review index is sticky: selecting a revision or comment jumps the document surface to its corresponding paragraph while keeping the decision context visible. Technical style diagnostics stay opt-in so ordinary readers see the document, not internal metadata.

For a source checkout:

```bash
python -m scripts.review_server long-wd --host 127.0.0.1 --port 8876
```

### Temporary phone collaboration over Tailscale

Run the review server on the machine that owns the workdir:

```bash
docx2typed review long-wd --tailscale --port 8876
```

The command queries `tailscale ip -4`, binds only to that exact Tailscale
address, and prints the phone URL, for example
`http://100.x.y.z:8876/`. Open that URL on a phone signed in to the same
tailnet. The browser and desktop/agent clients share the same server state;
the review page polls for new snapshots and queued decisions.

This is an interim private-network mode, not a public deployment:

- keep Tailscale ACLs restricted to the intended collaborators;
- do not replace `--tailscale` with `--host 0.0.0.0`;
- the transport is plain HTTP inside the tailnet, so do not expose the port
  outside Tailscale.

Tailscale must be installed and its CLI must be available on `PATH`.

The browser surface is deliberately semantic and reader-first:

- paragraph flow remains continuous instead of becoming isolated evidence cards;
- tracked insertions/deletions retain their inline location and metadata;
- comment records show real text and anchor paragraphs;
- style ownership comes from the Word style registry and `styles.json`;
- unsupported Word layout details are not silently rewritten; final fidelity is checked in the built DOCX.

### Screenshots

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

## Common workflows

### Clean text edit

```bash
docx2typed extract input.docx -o workdir
docx2typed view workdir --mode clean
# edit workdir/edit.md within one style region
docx2typed edit sync workdir --no-track
docx2typed build workdir -o edited.docx
docx2typed verify workdir edited.docx
```

### Tracked edit

```bash
docx2typed extract input.docx -o tracked-wd
# edit tracked-wd/edit.md
docx2typed edit sync tracked-wd --track --author "AI Reviewer"
docx2typed build tracked-wd -o tracked.docx
docx2typed verify tracked-wd tracked.docx
```

The output contains real `w:ins`/`w:del` nodes. Existing revisions are left untouched; new revisions receive the session author/date.

### Single revision decision

Read `revisions.json` first. A revision key has the form `part|kind|w:id|fingerprint`:

```bash
docx2typed decide accept "word/document.xml|insert|8|..." \
  --workdir tracked-wd --fingerprint "..."

docx2typed decide reject "word/document.xml|delete|7|..." \
  --workdir tracked-wd --fingerprint "..."
```

The fingerprint is a defensive check against stale review selections.

### Wholesale settlement

```bash
docx2typed decide accept-all \
  --workdir tracked-wd \
  --output accepted.docx \
  --workdir-out accepted-wd

docx2typed verify accepted-wd accepted.docx
```

`reject-all` has the same shape. The source workdir is never mutated; the new workdir is a clean baseline.

### Comment review

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

### Table structure

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

### Unicode vertical normalization

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

### Content-control text

Content-control paragraphs are exposed as IDs such as `S0.P0`. Edit their text through `edit.md` or MCP exactly like body text. The control properties (`w:sdtPr`) stay locked; structural insertion/removal of the control itself is outside this contract.

## MCP integration

After a source install, configure any stdio MCP host with:

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

After a tagged release is available on PyPI, the isolated one-line form is:

```bash
claude mcp add docx2typed -- uvx docx2typed mcp
```

For an MCP host JSON configuration, use:

```json
{
  "mcpServers": {
    "docx2typed": {
      "command": "uvx",
      "args": ["docx2typed", "mcp"]
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

## Workdir artifacts

| File | Purpose |
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

## Verification and development

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

## Longer article example

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

heading = doc.add_heading("A verifiable document review workflow for research writing", level=0)
heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

subtitle = doc.add_paragraph()
run = subtitle.add_run("A long-form fixture for testing structure, formatting, revisions, and comment boundaries")
run.italic = True

abstract = (
    "Long technical documents often contain body text, heading levels, font changes, superscripts, comments, "
    "revisions, tables, and pagination markers at the same time. If an editor turns a DOCX into plain text, "
    "readers may see the sentences but cannot tell whether a rewrite crossed a style boundary or whether untouched "
    "drawings, comment anchors, and package XML survived. This example splits document handling into six continuous "
    "stages—extract, read, edit, review, build, and verify—and records each stage's inputs, outputs, and hash evidence."
)
doc.add_heading("Abstract", level=1)
add_body(doc, abstract)

doc.add_heading("I. Problem background", level=1)
add_body(
    doc,
    "When researchers process experiment reports, patent specifications, grant applications, and theses, the common "
    "risk is not a missing adjective. It is a quiet change to the source document's formatting and structure after a "
    "text edit. A centered heading can become left-aligned, a superscript variable can become ordinary text, an empty "
    "table cell can inherit a neighbor's text, and a comment range can break when paragraphs are rebuilt. The difficulty "
    "is that text semantics and WordprocessingML structure coexist: people need continuous prose, while the tool must "
    "preserve every boundary that can be verified."
)


doc.add_heading("II. A verifiable intermediate representation", level=1)
add_body(
    doc,
    "The typed workdir is not a temporary text cache. It is an intermediate representation with paragraph IDs, style "
    "regions, a baseline hash, and source-part metadata. The clean projection supports a quick read-through; the style "
    "projection exposes font, size, color, superscript, and other style ownership; the raw projection puts paragraph "
    "markers, table references, revision nodes, comment anchors, and opaque structural nodes back into view. Before an "
    "edit is accepted, the engine checks whether it owns one style region; a mixed-region rewrite receives a split "
    "suggestion instead of a guessed style."
)


doc.add_heading("III. The boundary between revisions and comments", level=1)
paragraph = doc.add_paragraph()
paragraph.add_run("A revision is a change that can be decided; a comment is an instruction from an external reviewer. ").bold = True
paragraph.add_run(
    "In track mode, a replacement creates corresponding deletion and insertion revisions, with author and date written "
    "to the Word revision nodes. In comment review, the tool reads the comment text and anchors, performs the requested "
    "body edit, and keeps the comment itself by default. The teacher or user can still see the original instruction in Word "
    "and decide when to clean it up. Only an explicit comment-delete operation removes the comment entry, range anchors, "
    "and reference."
)


doc.add_heading("IV. Format markers and real output", level=1)
add_body(
    doc,
    "Format markers must not only look right in a browser. Bold, italic, strikethrough, fonts, sizes, colors, shading, "
    "superscript, underline, paragraph alignment, page-break markers, tables, and content controls all travel through "
    "the same structural path. The browser review console helps people locate paragraphs and revisions; the final DOCX is "
    "reassembled by build, checked independently by verify, and opened through LibreOffice for interoperability. When a "
    "browser cannot reproduce Word layout completely, the structure is not silently flattened; the delivery decision stays "
    "at the DOCX evidence level."
)


doc.add_heading("V. A small experiment table", level=1)
table = doc.add_table(rows=1, cols=3)
table.style = "Table Grid"
for cell, value in zip(table.rows[0].cells, ["Stage", "Input", "Inspectable evidence"]):
    cell.text = value
for row in [
    ("Extract", "source.docx", "source/template fingerprint"),
    ("Edit", "edit.md", "region ownership + run evidence"),
    ("Review", "revisions.json", "revision key + comment IDs"),
    ("Deliver", "output.docx", "verify + LibreOffice"),
]:
    cells = table.add_row().cells
    for cell, value in zip(cells, row):
        cell.text = value


doc.add_heading("VI. Conclusion", level=1)
add_body(
    doc,
    "Verifiable document editing is not about making a Word file resemble text. It gives every text change a clear scope, "
    "state, evidence trail, and rollback boundary. Once read, edit, review, build, and verify become one continuous chain, "
    "format fidelity no longer depends on a lucky import/export or on manually checking the entire file after every paragraph. "
    "This fixture is intentionally long so fixed-index jumps, continuous scrolling, style diagnostics, and final delivery checks "
    "can be observed at a realistic document length."
)

# Add explicit format markers to the final paragraph.
marker = doc.add_paragraph("Marker probe: ")
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

## Documentation map

- [`SKILL.md`](SKILL.md) — agent-facing contract and branch table.
- [`capabilities.md`](capabilities.md) — every CLI atom and MCP tool with exact syntax and exit contracts.
- [`composites.md`](composites.md) — seven workflows and end-to-end playbooks.
- [`verification.md`](verification.md) — freshness, fail-closed, byte-fidelity, and interoperability gates.
- [`docs/rpr-reference.md`](docs/rpr-reference.md) — Word `rPr` XML to style translation notes.

## Quick reference

1. Install with `python -m pip install -e .`.
2. Extract without mutating the source: `docx2typed extract input.docx -o workdir`.
3. Read `view --mode clean` once; inspect `style` or `raw` only where needed.
4. Edit `edit.md` inside the regions listed by `regions.md`.
5. Synchronize with `edit sync --no-track` or `edit sync --track --author NAME`.
6. Build, independently verify, and run a LibreOffice interoperability check.
7. Use `docx2typed-review workdir --port 8876` for the fixed review index and paragraph jumps.
8. Use MCP for region-scoped agent edits, revision decisions, comment inventory, collaboration, table operations, build, and verification.

The safe default is conservative: comments stay, ambiguous style ownership fails closed, source workdirs are not mutated by structural operations, and no document is called finished until the output evidence is green.
