# PRD: Complete revision settlement (accept-all that actually settles)

> **Status:** contract draft. Source: gap analysis 2026-08-06 (user review of the
> patent demo: "accept-all" leaves 102 revisions unsettled; many paragraphs
> locked by harmless cached markup). ADR 0037 (revisions), 0019 (opaque nodes),
> 0038 (containers) govern.

## Problem Statement

`docx2typed decide accept-all` promises to accept every tracked revision but
only settles the editable surface. On the user's revision-heavy patent,
52 revisions are settled and **102 remain** — inside paragraphs the engine
treats as unsupported: fields, math, drawings, content controls, format
history (rPrChange), and paragraphs merely containing `w:lastRenderedPageBreak`
(Word's cached pagination marker, rewritten on every save). From the user's
perspective "accept-all" is a lie: the output still shows tracked changes, and
the only way to clean it is editing in Word manually.

Two compounding causes:

1. **Overly coarse locking.** `w:lastRenderedPageBreak` is a harmless cache
   marker, not content, yet it forces the whole paragraph opaque
   (`paragraph-contains-unsupported-node`), taking its revisions hostage.
   `w:rPrChange` paragraphs (40 in the patent) are locked entirely — even
   plain text edits are rejected, though Word allows editing their text while
   keeping the format history.
2. **No settlement path for genuinely unsupported content.** Paragraphs
   containing fields/math/drawings/content controls cannot be re-rendered from
   the AST, so their revisions can never be accepted or rejected. But
   settlement does not require re-rendering: `w:ins`/`w:del`/`w:moveFrom`/
   `w:moveTo` are well-formed XML ranges in the raw bytes. Accepting an
   insertion is removing the wrapper, keeping the content; accepting a
   deletion is removing the range; rejecting is the inverse (with
   `w:delText` → `w:t` for deletions). This is a byte-range operation that
   never needs to understand the opaque interior.

## Solution

`accept-all`/`reject-all` settle **every revision in the document**, by
combining the existing AST-based settlement (editable surface) with a new
byte-range settlement pass over raw XML for everything else, and by widening
the editable surface where the lock was over-broad:

- **Harmless cache markers become inline markers.** `w:lastRenderedPageBreak`
  (and any future same-class markers: `w:proofErr` is already handled) parse as
  `InlineNode`s instead of opaque runs, so paragraphs containing them join the
  editable surface and their revisions settle through the normal path.
- **rPrChange paragraphs: text editable, format history locked.** A paragraph
  carrying `w:rPrChange` becomes editable for text (track/direct), while the
  rPrChange element itself remains a locked inline marker (never synthesized,
  never dropped, replayed byte-exact).
- **Byte-range settlement for the remaining opaque surface.** For paragraphs
  that still cannot be re-rendered (fields, math, drawings, content
  controls), accept/reject operates on the raw document XML: locate every
  revision container range in the paragraph bytes, then apply
  unwrap/remove/text-tag-switch edits from innermost to outermost. No opaque
  content is parsed or synthesized — only the revision wrapper bytes change.
- **Accept-all reports honestly.** After settlement the output contains zero
  tracked revisions (or a per-revision reason when a container cannot be
  resolved safely — expected to be an empty set in v1). The CLI prints the
  settled count and the new baseline workdir's inventory; the new baseline
  `revisions.json` must be empty.

## User Stories

1. As a patent author, I want `accept-all` to produce a final draft with zero
   tracked changes, so that I can hand the document to colleagues without
   explaining leftover markup.
2. As a patent author, I want paragraphs containing pagination cache markers
   to be editable, so that revision-heavy sections near page breaks are not
   locked out.
3. As a patent author, I want to edit text in paragraphs whose formatting was
   changed with tracked formatting (rPrChange), so that I can fix wording
   without losing the format history.
4. As a patent author, I want tracked changes inside fields/math/drawing
   paragraphs to be accepted or rejected by accept-all, so that the final
   document is clean even in structurally complex sections.
5. As a reviewer, I want to reject individual revisions inside opaque
   paragraphs (restoring deleted text), so that partial decisions work
   everywhere, not only on the editable surface.
6. As an agent operating via MCP, I want `decide_all` to return the number of
   revisions actually settled and the count of any residuals, so that I can
   verify the outcome without parsing the DOCX.
7. As a quality engineer, I want byte-range settlement to never touch opaque
   interior bytes, so that fields/math/drawings survive accept-all
   byte-identical.
8. As a quality engineer, I want a no-op round trip (extract → accept-all →
   verify) on the revision patent to pass with an empty new-baseline
   inventory, so that the contract is regression-proof.
9. As a maintainer, I want the settlement pass to be a single code path for
   both `accept` and `reject` with symmetric mapping, so that the mapping
   table stays the single source of truth.
10. As a user of LibreOffice/Word, I want the settled document to open without
    repair, so that the byte-range edits are structurally valid.

## Implementation Decisions

- **Settlement mapping (single source of truth, applies to both AST nodes and
  byte ranges):**
  - accept insert/move_to → unwrap (remove container tags, keep children)
  - reject insert/move_to → remove container and children
  - accept delete/move_from → remove container and children
  - reject delete/move_from → unwrap container; `w:delText` becomes `w:t`
- **Byte-range pass** (`settle_opaque_revisions`): operates on raw
  `word/document.xml` (and part XML for headers/footers/notes), locating
  revision containers by byte range from the existing locator machinery
  (tags are already discovered by `_TAG_RE`). Nested ranges resolve
  innermost-first so outer unwrap/remove stays consistent. Opaque interior
  bytes are copied verbatim between range boundaries. Runs only on paragraphs
  that did not settle through the AST path.
- **Surface widening:**
  - `w:lastRenderedPageBreak` joins the inline-marker set (like
    `w:footnoteRef`/`w:separator`): parsed as `InlineNode`, rendered from its
    token raw bytes, never synthesized.
  - `w:rPrChange`: the paragraph becomes editable; the rPrChange element is a
    locked inline marker attached to the run (existing opaque-marker
    mechanism, extended to allow the paragraph to remain editable while the
    marker itself is protected). Format-history bytes replay verbatim.
- **Decide-all integration:** `_decide_all` runs AST settlement (editable
  paragraphs), then byte-range settlement (remaining opaque paragraphs), then
  re-extracts the baseline as today. `decisions.json` records both passes with
  per-revision reasons. `revisions.json` of the new baseline must be empty in
  v1; if any container cannot be resolved, that paragraph is rejected with a
  `settlement-unsupported` reason (empty in expected fixtures).
- **Single decision path:** `accept_revision`/`reject_revision` on an opaque
  paragraph routes to the byte-range pass with the same key+fingerprint
  addressing; the AST node (if any) and byte range stay consistent via
  fingerprint checks.
- **No schema changes** to the typed grammar: `lastRenderedPageBreak`/
  rPrChange markers are existing node kinds (`docx-inline`, opaque marker);
  no new typed.md syntax.

## Testing Decisions

- **External-behavior tests only:** assert on the built DOCX and the new
  baseline inventory, never on internal pass structure.
- **Highest seam: the CLI contract** `decide accept-all` → new workdir with
  empty `revisions.json`, `verify` green, LibreOffice open (no-repair
  conversion succeeds). One seam covers AST + byte passes together.
- **Fixture suite:** extend the existing revision fixture
  (`tests/test_revisions.py::make_revision_docx`) with an opaque-revision
  paragraph (field + tracked insertion inside), an rPrChange paragraph, and a
  `lastRenderedPageBreak` paragraph; the user's revision patent stays the
  acceptance fixture (already copied into demo/tests fixtures).
- **Byte-fidelity assertions:** for opaque paragraphs, accept-all output bytes
  equal source bytes except the revision wrapper ranges; a dedicated test
  compares the field/math interior verbatim.
- **Symmetry test:** `accept` then `reject`-equivalent round trip per mapping
  row; `reject` of a deletion restores text with `w:t` (not `w:delText`).
- Prior art: `tests/test_decisions.py`, `tests/test_revisions.py`,
  `tests/test_parts.py` (same workdir → build → verify seams).

## Out of Scope

- Comment (w:comment) decisions — separate mechanism, own PRD.
- Editing table/row/cell structure, moving content between containers.
- Content controls / custom XML / OLE objects: still opaque, but their
  enclosing paragraphs' revisions now settle byte-wise; the objects
  themselves are never parsed or rewritten.
- Word HITL verification of settlement output (blocked on a Word
  installation; LibreOffice automated check is the CI seam, a manual Word
  check list is provided with the acceptance run).

## Further Notes

- The patent's 40 rPrChange paragraphs and ~13 lastRenderedPageBreak
  paragraphs are the bulk of the 102 residuals; after this PRD the expected
  residual count is 0. The demo HTML's section 04 wording ("102 retained")
  must be updated after implementation.
- `w:moveFrom`/`w:moveTo` byte settlement follows the same mapping; move
  pairs keep their w:id linkage only for inventory, not for settlement.
- The byte pass reuses the locator's tag stream; no new XML parsing
  dependency.
