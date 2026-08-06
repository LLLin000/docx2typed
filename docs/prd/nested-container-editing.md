# PRD: Nested container editing — table cell paragraphs

> **Status:** contract draft (ADR 0006 lineage; issue #8 decision: in scope as an
> independent slice with its own PRD).
>
> **Status review (2026-08-06):** after R1–R3 (revision support, #12–#15), the
> remaining opaque surface is exactly tables, headers, footers, footnotes,
> endnotes, text boxes, and unsupported run content. This PRD covers tables
> first as the most common structured container; the other containers are v2.
>
> **Positioning:** docx2typed is a general-purpose DOCX editor (CLI + MCP), not
> a patent-specific tool. The editable surface must cover every place prose
> lives in a real Word document: body paragraphs (done), table cells (this
> PRD), and the remaining containers (v2). Revisions, paragraph marks, and
> anchors behave identically everywhere the surface reaches.

## Problem Statement

The editable surface is strictly `w:body` direct paragraphs (ADR 0006). Tables
(`w:tbl > w:tr > w:tc > w:p`) are byte-preserved but opaque: their cell
paragraphs cannot be edited through `edit.md`, `replace_text`, or `batch_edit`.
In real Word documents tables are ubiquitous — reports, forms, invoices,
scientific papers, claim sections — and cell prose is routinely edited. Today
any change there requires editing the DOCX elsewhere and re-extracting, which
breaks the hash-bound freshness contract of the workdir and forces users out of
the engine's governance.

R1–R3 structured revision containers inside direct paragraphs; table cells are
the first structurally regular container to join the surface. Headers, footers,
footnotes, endnotes, and text boxes are independent parts or irregular nesting
and are deferred to v2 (their revisions are already inventoried read-only by
`scan_package_revisions`).

## Scope

### v1 (this PRD)

- Table cell paragraphs: `w:tbl` direct children of `w:body` (including nested
  tables inside cells, cells spanning rows).
- Cell paragraphs join the editable surface: `typed.md`, `edit.md`, `regions.md`,
  `revisions.json/md`, `edit sync`, `build`, `verify`, MCP tools.
- Cell paragraphs may carry revision containers (R1 grammar), paragraph marks,
  and comment/bookmark anchors; revision semantics (R2) apply unchanged.
- Row/column/cell structure (`w:tblPr`, `w:trPr`, `w:tcPr`, grid, spans) stays
  byte-preserved and is not editable; only cell paragraph text is editable.

### v2 (explicitly out of this PRD)

- Headers, footers, footnotes, endnotes, text boxes, content controls, custom
  XML, fields, math, drawings inside cells (already opaque; unsupported run
  content inside cell paragraphs stays opaque per ADR 0019). Each v2 container
  gets its own PRD; the locator/patch/verify machinery built here is the
  foundation for all of them.

## Contract

### Identity

- Body paragraphs keep `P{index}` ids and `original_index` (unchanged).
- Table cell paragraphs get stable ids derived from their container path:
  `T{t}.R{r}.C{c}.P{p}` where `t` is the body table ordinal, `r`/`c` are row
  and cell ordinals within that table (nested tables get their own `T` ordinal
  relative to the containing cell), and `p` is the cell paragraph ordinal.
- `original_index` for cell paragraphs is `-1` (they are not body slots); a new
  `container_path` field on `ParagraphSlice`/`Paragraph` carries
  `("tbl", t, "tr", r, "tc", c, "p", p)` ordinals used by the byte patch.

### Byte patch

- The locator (`locate_document_xml`) additionally finds table cell paragraphs
  and their enclosing table byte ranges.
- `patch_document_xml` replaces body-paragraph bytes as today; table byte ranges
  are re-rendered recursively from their cell paragraphs (touched cells render,
  untouched cells replay raw bytes — byte-exact no-op stays byte-exact).
- Table structure bytes (`w:tblPr`, `w:tblGrid`, `w:trPr`, `w:tcPr`, spans,
  `w:gridSpan`, `w:vMerge`) are never synthesized; they come from the template
  byte range.

### Surface

- `edit.md` projects cell paragraphs with their `T…` ids in document order
  (tables interleaved with body paragraphs in body order); `@new`/`@delete`
  markers on cell paragraphs are rejected in v1 with
  `table-structure-immutable` (adding/removing table rows is out of scope).
- Style ownership, revision boundaries, and gap/insert placeholders apply to
  cell paragraphs exactly as to body paragraphs (R2 policy).
- MCP tools (`get_paragraph`, `replace_text`, `batch_edit`, `insert_paragraph`,
  `delete_paragraph`) accept `T…` ids; `insert_paragraph`/`delete_paragraph`
  reject table ids with the stable diagnostic.

### Verification

- Three verify layers extend to cell paragraphs (final/original/structure
  signatures per paragraph, including marks and revisions).
- `validate_workdir` baseline checks cover cell paragraphs (skeleton, tokens,
  styles, marks) against the template-derived baseline.
- Package integrity (ADR 0004) unchanged; the table byte range is covered by the
  per-paragraph byte checks.

### Governance

- A new ADR 0038 records the nested-container editable surface, superseding the
  "direct body paragraphs only" reading of ADR 0006 for table cells.
- Existing ADRs 0013 (byte-preserving patch), 0019 (opaque nodes), 0021
  (workdir project) are extended, not weakened.

## Out of scope (v1 hard boundaries)

- Editing table structure (insert/delete rows/cells, merge/split, widths).
- Headers/footers/footnotes/endnotes/text boxes (v2, own PRDs).
- Cross-table edits spanning multiple cells in one hunk (region boundary rules
  apply per cell paragraph).
- Document-level section properties inside tables.

## Acceptance

1. Extract a general table-bearing DOCX fixture (python-docx: 2×2 table with
   styled cells, merged-cell span, nested table, one cell with a tracked
   insertion): workdir validates; `edit.md` shows `T0.R0.C0.P0` etc.; no-op
   build is byte-identical.
2. Edit a cell paragraph through `edit.md sync` and through MCP `replace_text`;
   build + verify pass; the table renders with the new text; untouched cells
   are byte-identical.
3. Track-mode edit inside a cell paragraph emits `w:ins`; direct-mode edit of
   revision text inside a cell is rejected with the standard diagnostic.
4. `@delete` / `insert_paragraph` on a `T…` paragraph fails with
   `table-structure-immutable`.
5. LibreOffice opens the edited output without repair; Word HITL pending.
6. Full suite (144 baseline) stays green.
