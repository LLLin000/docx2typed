# PRD: Deep byte-level XML surgery layer (shared walker)

Status: draft · 2026-08-07 · typed-mode branch

## Problem Statement

Adding each new Word structure feature to docx2typed (tracked revisions →
comment decisions → table structure ops → content controls) keeps requiring a
fresh hand-rolled XML byte scan + splice pass inside the structural core. The
discipline — walking raw OOXML bytes while correctly handling namespace
prefixes, self-closing tags, CJK byte-vs-str offsets, and nested containers —
is implicit and has been re-implemented independently at least five times.
Each re-implementation ships its own variant of the same edge-case bugs:

- nested `pPr` truncation (`_raw_p_parts`, exposed by the corpus)
- CJK text byte-offset corruption (`_raw_p_parts`)
- freestanding comment/bookmark anchor mis-attribution
- inserted table rows duplicating header text into the new baseline
  (`_clone_row_with_empty_cells`, fixed 2026-08-07)
- `malformed table XML nesting` boundary cases (`_locate_table_elements`)

The clone/splice helpers (`_clone_row_with_empty_cells`,
`_clone_cell_with_empty_paragraph`, `_merge_cell_bytes`, `_split_cell_bytes`)
each hand-roll "keep the structure, clear the text" slicing, and the part
locator returns a 7-position tuple that grows by accretion. The cost of the
next Word structure feature is another scan pass + another splice set, not
another thin specification.

## Solution

Extract one deep module that owns the shared byte-walking discipline, and
make every existing scan pass a thin consumer of it. Public behavior does not
change: same function signatures, same byte outputs, no new CLI or MCP
surface. The refactor is validated per migration step by byte-level diffs
against current behavior, then the existing regression gates.

Concretely:

- A token-level walker over raw `word/document.xml`/part bytes yields
  (tag name, open/close/self-close, byte range) with a nesting stack — the
  single owner of bytes-vs-str offsets, namespace-prefix handling,
  self-closing detection, and nested-container depth.
- The five scan passes (`locate_document_xml`, `locate_part_xml`,
  `_raw_p_parts`, `_locate_table_elements`, `settle_xml_revisions`) migrate
  one at a time onto the walker; each keeps its signature and byte output.
- The splice helpers become composable range transforms over walker ranges
  (clone-with-empty-text, gridSpan merge/split, anchor re-anchoring).
- `locate_part_xml` returns a named structure (root range, paragraphs, entry
  ids, table ranges, cell paragraphs, entry ranges) instead of the 7-tuple.

## User Stories

1. As a developer adding the next structural operation (move revisions,
   section delete, paragraph move), I want to express it as a thin
   specification over a shared walker, so that I do not re-derive the
   scan/splice discipline from scratch.
2. As a maintainer fixing a byte-level bug, I want the edge cases (bytes-vs-
   str, self-closing, prefixes, nesting) owned in exactly one place, so that
   the fix lands once and every operation inherits it.
3. As a test author, I want the walker's unit tests to cover the shared
   discipline, so that per-operation tests stop re-testing the accidents of
   the current code.
4. As the corpus maintainer, I want each pass migration gated by a byte-level
   diff against current behavior, so that no behavior drifts during the
   refactor.
5. As a reviewer of the part locator contract, I want a named structure
   instead of a growing positional tuple, so that adding a field breaks
   loudly at the call site rather than silently reordering values.
6. As a user of existing commands, I want `decide`, `table-*`, comment
   deletion, and build/verify to behave exactly as before, so that the
   refactor is invisible.
7. As a LibreOffice/Word consumer, I want outputs of table and revision
   operations to keep opening cleanly, so that the structural layer stays
   interoperable.
8. As an agent using the MCP server, I want table and comment tools to keep
   working unchanged, so that the refactor does not touch the tool surface.
9. As the future author of a content-control structure op, I want to reuse
   the same range-transform primitives, so that sdt edits stop being a
   separate code path.

## Implementation Decisions

- **One deep walker module** (internal, not public): token stream + nesting
  stack + byte-range API over raw bytes. It is the only code that decodes
  `_TAG_RE`-style tokens, classifies open/close/self-close, maps namespaces,
  and computes byte offsets; it exposes named range helpers rather than raw
  index arithmetic.
- **Signature-stable migrations**: each scan pass migrates one at a time and
  keeps its existing signature and byte output. Migration order:
  `locate_document_xml` → `locate_part_xml` → `_raw_p_parts` →
  `_locate_table_elements` → `settle_xml_revisions`.
- **Named part layout**: the part locator returns a dataclass (root range,
  paragraph slices, entry ids, table ranges, cell paragraphs, entry ranges)
  mirroring the existing body-side `DocumentSlices` pattern. The positional
  7-tuple is removed; all callers update in the same change.
- **Composable range transforms**: clone-with-empty-text, gridSpan
  merge/split, and anchor re-anchoring are expressed as operations on walker
  ranges; the "keep structure, clear text" logic exists once, not once per
  helper.
- **No new CLI/MCP surface, no schema changes, no workdir format changes.**
- **No behavior changes**: no-op build stays byte-identical; settled,
  comment-cleared, and table-patched outputs match current bytes exactly.
- ADR 0013 (byte-preserving document patch) and ADR 0015 (XML whitespace
  preservation) are respected and supported by centralizing the discipline;
  no ADR is reopened.

## Testing Decisions

- **External behavior first**: good tests assert byte outputs of each
  transform and the end-to-end extract → decide/table-op → build → verify
  flow, never the internal walker's presence.
- **New walker unit tests** cover the shared discipline: CJK byte offsets,
  self-closing roots, namespace-prefixed tags, deeply nested containers
  (3-level tables, sdt inside table cells, paragraphs with pPr-embedded
  pPrChange), freestanding anchors between elements.
- **Per-operation tests stay** but stop re-testing the accidents; existing
  assertions (e.g. table op row/col counts, empty inserted rows, byte
  round-trips) remain as regression nets.
- **Migration gate per pass**: snapshot current byte output → migrate →
  byte-diff identical → full pytest green → corpus acceptance 10/10 →
  LibreOffice conversion clean.
- **Prior art**: pure-function tests in the table suite (ops applied to the
  original 2x2 independently), part locator tests, the acceptance corpus
  runner, and the CLI/MCP tool smoke suite.

## Out of Scope

- Candidate 02 (edit.py ↔ edit_sync.py lazy-import cycle and the triplicated
  projection-format escaping) — separate PRD.
- Candidate 04 (unified structural-decision publish adapter) — depends on
  this layer landing first; separate PRD.
- New Word structure features (move revisions, section operations, sdt
  structure ops) — this PRD only prepares the layer they ride on.
- Performance work — no O(n²) regressions are allowed, but no optimization
  is in scope.

## Further Notes

- The most recent instance of the bug class this PRD prevents is commit
  `5a025d8` (insert-row clone copying header text), fixed before this PRD
  was drafted.
- The walker is an internal module; the interface-is-the-test-surface
  principle applies to the transform functions that keep public signatures.
- The architecture review (2026-08-07) that produced this PRD also recorded
  candidates 02 and 04 as separate projects with their own friction evidence.
