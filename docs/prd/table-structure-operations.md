# PRD: Table structure operations (row/col insert-delete, cell merge/split)

> **Status:** implemented (commit `6f92c36`). Source: goal-mode gap analysis
> 2026-08-06 — "对标 Word 的 DOCX 编辑体验：补齐表格结构操作". ADR 0013
> (byte-preserving patch), 0021 (workdir project), 0038 (containers) govern.

## Problem Statement

Tables are fully editable at the paragraph level (N1) but structurally frozen:
no CLI/MCP operation can insert or delete rows/columns or merge/split cells.
Users must rebuild tables in Word. Structure operations are byte-level edits
to the raw table XML — the paragraph AST is untouched, but the container
bounds change, so the operation must re-extract a fresh baseline from the
patched template.

## Solution

Byte-level table surgery in `scripts/typed_docx.py`:

- `apply_table_operation(xml, slices, table_ref, op, args)` dispatches on op:
  - `insert-row <after>`: `_locate_table_elements` finds the target table's
    `<w:tr>` bounds; `_clone_row_with_empty_cells` clones the referenced row
    with empty cell paragraphs; the row-level gap cursor bug (duplicated
    header when the gap cursor was not advanced in cell-surgery branches) is
    fixed; the cloned row lands after the `</w:tr>` of `after` (row 0 case
    keeps the table's header gap).
  - `delete-row <row>`: removes the row bytes plus its trailing gap
    (`</w:tr><w:tr>` leak when deleting row 0 fixed by consuming the gap).
  - `insert-col <after>`: clones the cell of every row at `after`, inserting
    `</w:tc><w:tc>…` (delete-col's leak of `</w:tc><w:tc>` after the skipped
    first cell fixed by consuming it).
  - `delete-col <col>`: removes each row's cell at `col` including the gap.
  - `merge-cells <row> <col> <span>`: keeps the first cell, removes the
    following `span-1` cells, sets `w:gridSpan=span` on the survivor
    (`_merge_cell_bytes`; merge branch previously dropped the cell gap).
  - `split-cells <row> <col> <span>`: removes `w:gridSpan`, re-inserts
    `span-1` empty cells (`_split_cell_bytes`).
  - `<w:tcW>` boundary hazard: `_find_open_end` requires `(?=[ >])` so it does
    not match inside `w:tcW`.
- `_locate_table_elements` returns per-row/per-cell byte ranges, tolerating
  absent `w:tr`/`w:tc` (vMerge continuation rows have no `w:tcPr`).

**CLI**: `decide table-insert-row T0 --workdir wd --output out.docx
--workdir-out wd2 --args "1"` — new-baseline path via `_apply_table_op` in
`scripts/decisions.py`: apply op to the template, re-extract a fresh workdir,
verify; the source workdir is untouched. One op per table per call.

**MCP**: six tools — `table_insert_row`, `table_delete_row`,
`table_insert_col`, `table_delete_col`, `table_merge_cells`,
`table_split_cells` — via `_table_op_tool`; each takes `table_ref`, op
arguments, and `output`/`workdir_out`.

## Acceptance

- Row/col counts change exactly as specified on the original 2×2 fixture:
  insert-row → (3,6) paragraphs, delete-row 0 → (1,2), insert-col → (2,6),
  delete-col 0 → (2,2), merge (0,0,2) → (2,3), split (0,0,2) → (2,5).
- Output builds and verifies green; LibreOffice converts merge/split outputs
  with no warnings.
- Source workdir never mutated; `--workdir-out` holds the fresh baseline.

## Out of scope

- Cell width/height property editing, nested-table surgery, row span
  (vMerge) creation — paragraph-level editing inside cells already covers
  content.
