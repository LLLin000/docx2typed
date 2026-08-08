# capabilities.md — atoms (工具使用说明)

Atoms are single commands, near-deterministic, with no dependencies. Two
surfaces: the CLI and the MCP server — same engine, same gates. Workflows
that compose them: [`composites.md`](composites.md). Shared acceptance
contract: [`verification.md`](verification.md).

## Workdir lifecycle atoms (CLI)

| Atom | Purpose | Exit contract |
|---|---|---|
| `python -m docx2typed extract <input.docx> -o <workdir>` | Create a typed workdir from a DOCX (never mutates the source) | 0 + workdir; source/template fingerprints recorded |
| `python -m docx2typed validate <workdir>` | Grammar/skeleton/style/template integrity check | 0 only when workdir is valid AND edit state is clean |
| `python -m docx2typed view <workdir> --mode clean` | Read-only continuous-prose projection | stdout prose; 0 |
| `python -m docx2typed view <workdir> --mode style` | Read-only diagnostic projection showing style regions | stdout with style labels; 0 |
| `python -m docx2typed view <workdir> --mode raw` | Read-only projection with all typed tokens visible | stdout typed markup; 0 |

## Edit atoms (CLI)

| Atom | Purpose | Exit contract |
|---|---|---|
| `python -m docx2typed edit status <workdir>` | Freshness: `clean` / `dirty` / `stale-clean` / `conflict` | 0 for all four states |
| `python -m docx2typed edit refresh <workdir> [--init] [--discard]` | Regenerate `edit.md` from `typed.md` after a raw typed change; `--init` for legacy workdirs, `--discard` replaces a dirty draft | 0; every non-clean build gate uses the sidecar, not the header |
| `python -m docx2typed edit sync <workdir>` | Apply an edited `edit.md` draft to the canonical typed AST: unchanged text keeps style, rewritten text inherits the replaced region's style, insertions inherit caret context; cross-region rewrites rejected | 0 + new canonical state; every hunk recorded in `edit.state.json.run.json` |

Before syncing, read `regions.md` in the workdir — it lists style regions
with indices and auto-updates after every edit. Plan region-scoped edits
from it.

## Decision atoms (CLI)

`python -m docx2typed decide <action> --workdir <workdir> [options]`

| Action | Purpose | Key options |
|---|---|---|
| `accept <revision_key>` / `reject <revision_key>` / `reinsert <revision_key>` | Decide ONE tracked revision (key: `part|kind|w:id|fingerprint` from `revisions.json`); mutates the typed AST, publishes transactionally | `--fingerprint` defensive check, `--author`, `--text` |
| `accept-all` / `reject-all` | Settle every tracked revision at byte level; builds a new DOCX and re-extracts a fresh clean-baseline workdir | `--output <after.docx>`, `--workdir-out <new-wd>` — source workdir never mutated |
| `comment-delete <id>` | Delete one Word comment: `comments.xml` entry, all `commentRangeStart/End` anchors, `commentReference`s; other comments untouched | `--workdir`; publishes in place |
| `table-insert-row <T0>` / `table-delete-row <T0>` / `table-insert-col <T0>` / `table-delete-col <T0>` / `table-merge-cells <T0>` / `table-split-cells <T0>` | Row/col insert/delete, horizontal merge (gridSpan), split; table refs are `T0`, `T1`, … (see `view --mode raw`) | `--args '<index> [<index> <span>]'` (0-based), `--output`, `--workdir-out`; new clean baseline, source untouched |

## Normalization atoms (CLI)

| Atom | Purpose | Exit contract |
|---|---|---|
| `python -m docx2typed audit scan <workdir> -o <scan.json>` | Read-only: hash-bound candidate artifact for Unicode superscript/subscript vertical candidates | 0 + scan.json + run evidence; never mutates |
| `python -m docx2typed audit apply <workdir> --scan <scan.json> --policy <policy.json> -o <normalized.docx> --workdir-out <normalized-workdir>` | Apply an approved policy to a NEW DOCX + NEW workdir with `normalization.audit.json`; stale bindings fail before transform | 0 only with complete approved policy and matching fingerprints |
| `python -m docx2typed normalize <workdir> --legacy-policy-1 …` | Unaudited compatibility path; emits `governance_status="legacy-unaudited"` | Use `audit scan/apply` when approval matters |

## MCP atoms (server: `python -m docx2typed.mcp_server`)

Session tools:

| Tool | Purpose |
|---|---|
| `workdir_open(workdir, author?, track?)` | Open the session document; validates, reports freshness + effective edit mode. Call once first. |
| `workdir_status()` | Freshness state of the opened workdir |
| `revert()` | Discard the uncommitted draft, regenerate from canonical typed source |

Read tools:

| Tool | Purpose |
|---|---|
| `list_paragraphs()` | Draft paragraphs: id, visible-text summary, token count, deletions |
| `get_paragraph(paragraph_id)` | Draft text + style regions (region-scoped editing basis) |
| `diff_preview()` | Dry-run of `commit_sync`: hunks with style ownership, warnings |

Edit tools (region-scoped, zero guessing):

| Tool | Purpose |
|---|---|
| `replace_text(paragraph_id, old, new)` | Replace exactly one occurrence in one style region |
| `batch_edit(paragraph_id, edits)` | Multi-region edit, atomic, immediate |
| `insert_paragraph(after_id, text, inherit?)` | Insert a new paragraph in the draft |
| `delete_paragraph(paragraph_id)` | Mark a paragraph deleted (protected structure rejected at commit) |
| `commit_sync()` | Apply the draft to the canonical typed AST under the session edit mode, re-validate, publish |

Build/verify tools:

| Tool | Purpose |
|---|---|
| `build_docx(output?)` | Build the DOCX from the committed workdir (clean state required) |
| `verify_output(output)` | Independently verify a built DOCX against the workdir |

Decision tools:

| Tool | Purpose |
|---|---|
| `accept_revision(revision_key, expected_fingerprint)` / `reject_revision(revision_key, expected_fingerprint)` / `reinsert_deleted_text(revision_key, expected_fingerprint)` | One revision decision, fingerprint-defended |
| `decide_all(action, output, workdir_out)` | accept-all / reject-all byte settlement + new baseline |
| `delete_comment(comment_id)` | Delete one comment (entry + anchors + references) |

Table tools:

| Tool | Purpose |
|---|---|
| `table_insert_row(table_ref, after, output, workdir_out)` | Insert empty row after `after` (0-based) |
| `table_delete_row(table_ref, row, output, workdir_out)` | Delete a row |
| `table_insert_col(table_ref, after, output, workdir_out)` | Insert empty column after `after` in every row |
| `table_delete_col(table_ref, col, output, workdir_out)` | Delete a column from every row |
| `table_merge_cells(table_ref, row, col, span, output, workdir_out)` | Merge `span` cells horizontally via gridSpan |
| `table_split_cells(table_ref, row, col, span, output, workdir_out)` | Split one cell into `span` cells |

All table tools produce a new DOCX + clean-baseline workdir; the source
workdir is never mutated.

## Files that act as tools

- `regions.md` — style regions with indices (read to plan region-scoped
  edits; auto-updated after every edit).
- `revisions.json` / `revisions.md` — read-only tracked-revision inventory
  (type/author/date/text/location/editable); the source of `revision_key`s.
- `edit.state.json.run.json` — run evidence for extract/refresh/sync.
- `docs/rpr-reference.md` — rPr XML → style translation dictionary.
