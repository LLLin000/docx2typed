# 0038 — Nested-container editable surface: table cell paragraphs

## Status

Accepted (PRD `docs/prd/nested-container-editing.md`, issue #8 decision, N1 ticket #16).

## Context

ADR 0006 restricts the editable surface to `w:body` direct paragraphs. Real
Word documents place prose inside tables, and the engine's governance (hash
bound freshness, style ownership, revision semantics) cannot reach it. The
revision support slices (R1–R3, ADR 0037) structured revision containers
inside direct paragraphs; table cells are the first structurally regular
container to join the surface. Headers, footers, footnotes, endnotes, and
text boxes, headers, footers, footnotes, endnotes are editable (v2,
per-PRD) via `B{box}.P{p}` ids and `<!--@part key=...-->` partitions.

## Decision

Table cell paragraphs (`w:tbl > w:tr > w:tc > w:p`, including nested tables)
join the editable surface.

- **Identity**: cell paragraphs get `T{t}.R{r}.C{c}.P{p}` ids derived from
  their container path; body paragraph ids and `original_index` are
  unchanged; cell paragraphs carry a `container_path` ordinal tuple used by
  the byte patch.
- **Byte patch**: table byte ranges are re-rendered recursively — touched
  cells render from the AST, untouched cells replay raw bytes (no-op stays
  byte-exact); table structure bytes (`w:tblPr`, `w:tblGrid`, `w:trPr`,
  `w:tcPr`, spans) are never synthesized.
- **Surface semantics**: style ownership, revision boundaries, gap/insert
  placeholders, and three-layer verification apply to cell paragraphs
  exactly as to body paragraphs; paragraph insertion/deletion inside tables
  is rejected (`table-structure-immutable`) until row-level editing is
  designed.
- **Verification**: validate baseline checks and the three verify signatures
  cover cell paragraphs (skeleton, tokens, styles, marks, revisions).
- **Governance**: ADR 0006's "direct body paragraphs only" reading is
  superseded for table cells; ADRs 0013 (byte-preserving patch), 0019
  (opaque nodes), 0021 (workdir project), 0037 (revisions) are extended, not
  weakened.

## Consequences

- Locator, patch, projection, validate, verify, and the MCP tools gain
  table-awareness; the fixture corpus gains a table-bearing general-purpose
  DOCX.
- Row/column/cell structure editing, headers/footers/footnotes/endnotes and
  text boxes and header/footer/footnote/endnote paragraphs are editable;
  container structure operations (insert/delete rows, cells, paragraphs into
  containers) remain out of scope.
- Unsupported run content inside cells stays opaque (ADR 0019).

## Updates (goal mode 2026-08-06)

- **S1 comments**: `comments.xml` is an editable part; `decide accept-all`/
  `reject-all` clear all comments (anchors stripped byte-level, comments part
  emptied with original root tag); `decide comment-delete <id>` deletes one
  comment with tombstone + anchor re-anchoring. PRD
  `docs/prd/comment-decisions.md`.
- **S2 tables**: structural ops on the raw table bytes — `insert-row`,
  `delete-row`, `insert-col`, `delete-col`, `merge-cells` (gridSpan),
  `split-cells` — each re-extracts a fresh baseline workdir; source workdir
  untouched. PRD `docs/prd/table-structure-operations.md`.
- **S3 content controls**: body-level `w:sdt` joins the container surface;
  `sdtContent` direct-child paragraphs are `S{i}.P{n}`; sdt byte ranges are
  patch units, `sdtPr` structure replays byte-exact. PRD
  `docs/prd/content-control-editing.md`.
