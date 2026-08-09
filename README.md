# docx2typed

[中文版本](https://github.com/LLLin000/docx2typed-typed-mode/blob/main/README.zh-CN.md) · [安装与协作指南](https://github.com/LLLin000/docx2typed-typed-mode/blob/main/Installation.md)

> Structure-preserving DOCX text editing for agents, reviewers, and developers.

`docx2typed` edits the words in a `.docx` without flattening the document into lossy plain text or HTML. It keeps the document's formatting, anchors, comments, tracked revisions, tables, content controls, and untouched package parts in a typed workdir. Only requested text changes are written back.

<p align="center">
  <img src="docs/assets/review-console-revisions.png" alt="docx2typed review console showing tracked revisions and a fixed review index" width="100%" style="max-width:100%;height:auto;display:block">
</p>

## What it provides

| Need | docx2typed |
|---|---|
| Edit text safely | Extract a workdir while leaving the source `.docx` unchanged. |
| Preserve formatting | Keep style ownership, paragraph structure, anchors, and untouched package parts. |
| Review changes | Work with tracked revisions, comments, and paragraph-level navigation. |
| Deliver a DOCX | Build a new file, verify it independently, and check it with LibreOffice. |

This is a structure-preserving editing engine, not a browser replacement for Microsoft Word. The review console helps people inspect and decide changes; the built DOCX is the final deliverable.

## Install

Requirements: Python **3.11+**. LibreOffice Writer is recommended for the final interoperability check. Tailscale is optional and only needed for phone access.

### PyPI

```bash
python -m pip install --upgrade docx2typed

docx2typed extract --help
```

For an isolated command-line installation:

```bash
uv tool install --upgrade docx2typed
```

### Source checkout

```bash
git clone https://github.com/LLLin000/docx2typed-typed-mode.git
cd docx2typed-typed-mode
python -m pip install -e .
```

## Quick start

Extract a source document. The source file is not modified.

```bash
docx2typed extract input.docx -o workdir
docx2typed view workdir --mode clean
```

Edit `workdir/edit.md` inside the relevant text region, then synchronize and build a new DOCX:

```bash
docx2typed edit sync workdir --no-track
docx2typed build workdir -o edited.docx
docx2typed verify workdir edited.docx
```

For a tracked edit, synchronize with an author name instead:

```bash
docx2typed edit sync workdir --track --author "Reviewer"
docx2typed build workdir -o reviewed.docx
docx2typed verify workdir reviewed.docx
```

`verify` checks the output against the workdir. Open the resulting DOCX with Word or LibreOffice for the final visual check; text-length changes may naturally reflow lines and pages.

## Review console

The review console renders the typed workdir as a continuous document surface. Its fixed review index can jump to a revision or comment while the document stays in view.

### Standalone HTML

```bash
python -m docx2typed.review_console workdir -o review.html
```

Open `review.html` in a browser.

### Local review server

```bash
docx2typed-review workdir --host 127.0.0.1 --port 8876
```

Open <http://127.0.0.1:8876/> on the same machine.

### Temporary phone access

For short-lived collaboration on a private tailnet:

```bash
docx2typed review workdir --tailscale --port 8876
```

Open the printed URL on a phone signed in to the same Tailscale network. Keep access restricted to the intended collaborators and do not expose the review port to the public Internet.

<p align="center">
  <img src="docs/assets/review-console-desktop.png" alt="Desktop docx2typed review console showing a continuous document surface and fixed review index" width="72%" style="max-width:100%;height:auto;display:block">
</p>

## Review changes

### Accept or reject tracked revisions

A tracked edit creates real Word `w:ins` and `w:del` nodes. Existing revisions remain available for review. Individual and whole-document decisions write a new DOCX/workdir rather than changing the source workdir in place.

```bash
docx2typed decide accept-all \
  --workdir tracked-wd \
  --output accepted.docx \
  --workdir-out accepted-wd

docx2typed verify accepted-wd accepted.docx
```

### Comments

Comments are preserved by default. The tool may use a comment as an editing instruction, but comment IDs, authors, dates, text, and anchors remain in the output. Delete a comment only when the user explicitly requests it:

```bash
docx2typed decide comment-delete 1 --workdir workdir
```

### Tables and content controls

Text inside table cells and content-control paragraphs can be edited through `edit.md` or MCP. Dedicated table commands can insert, delete, merge, or split table structure without silently rewriting cell text. See the [capabilities reference](capabilities.md) for command syntax.

## MCP integration

After installation, add the stdio MCP server to Claude:

```bash
claude mcp add docx2typed -- uvx docx2typed mcp
```

For another MCP host, use:

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

The MCP server exposes workdir inspection, text editing, revision and comment review, table operations, build, and verification. It uses the same safety checks as the CLI.

## Scope and defaults

- Text editing is the default surface. Existing formatting and document structure are preserved rather than redesigned.
- Comments stay unless deletion is explicitly requested.
- Ambiguous formatting ownership is reported instead of guessed.
- Layout reflow caused by changed text is normal; final fidelity is checked in the built DOCX.

## Further reading

- [Installation and collaboration guide](Installation.md)
- [CLI and MCP capabilities](capabilities.md)
- [End-to-end workflows](composites.md)
- [Verification guarantees](verification.md)
