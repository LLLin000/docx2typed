# PRD: Content control (w:sdt) text editing

> **Status:** implemented (commit `bbbda78`). Source: goal-mode gap analysis
> 2026-08-06 — "对标 Word 的 DOCX 编辑体验：补齐内容控件文本编辑". ADR 0019
> (opaque nodes), 0038 (containers) govern.

## Problem Statement

Body-level rich-text content controls (`<w:sdt><w:sdtPr>…</w:sdtPr>
<w:sdtContent>…</w:sdtContent></w:sdt>`, e.g. form fields and templates)
currently make their paragraphs opaque (`paragraph-contains-unsupported-node`):
the text inside a control cannot be edited even though Word allows it, and the
control structure (`sdtPr` aliases/tags) must survive byte-exact.

## Solution

Treat a body-level `w:sdt` as a container whose `sdtContent` direct-child
paragraphs join the editable surface, like tables (N1) and text boxes (N2):

- **Locator.** `sdtContent` opening tags are recognized at `depth >
  body_depth + 1` when the open chain is `body > sdt > (sdtPr|sdtEndPr)*`
  (the `sdtContent` element itself is not yet on the stack at open time; the
  chain check validates `chain[0] == "sdt"` and every intermediate element is
  `sdtPr`/`sdtEndPr`). Direct-child `<w:p>` elements collect as
  `SdtSlice.cells` with path `("sdt", i, "p", n)` → ids `S{i}.P{n}`.
  Unclosed `sdtContent` fails validation.
- **Parse.** `_find_sdt_paragraph` resolves the container path against the
  parsed body; sdt paragraphs parse with the same pipeline as table cells
  (id, container_path, original_index -1).
- **Anchors.** `_attach_freestanding_anchors` includes sdt cell ranges so
  comment/bookmark anchors inside controls attach to the right paragraph.
- **Render.** `_render_sdts` replays the sdt region: touched paragraphs
  render from AST, untouched replay raw, the `sdtPr`/structure bytes always
  come from the template.
- **Patch.** sdt ranges join the top-level patch units (sorted with
  paragraphs/tables); the guard's protected-region set excludes sdt ranges so
  edits do not trip `protected document XML region changed`.

## Acceptance

- `S0.P0` appears in typed.md; no-op build is byte-identical.
- Editing the control text (edit.md → sync → build) changes only the text;
  `w:sdtPr`/`w:alias` structure byte-exact; verify green.
- LibreOffice opens the edited output without warnings.

## Out of scope

- sdt inside table cells/headers (nested controls) — body-level only in v1;
  the locator fails loudly (validation error) rather than silently dropping.
- Control properties (dropdown items, date format) — text only.
- Deleting/inserting whole controls — paragraph-level ops inside a control
  only.
