# PRD: Agent Editor Facade (virtual-file editing surface)

Status: draft · 2026-09-06 · branch `feature/agent-editor-facade`

## Problem Statement

The MCP surface exposes 36 flat, paragraph-scoped tools (`list_paragraphs`,
`get_paragraph`, `replace_text`, `batch_edit`, `insert_paragraph`,
`delete_paragraph`, …). The abstraction level is wrong for the primary
consumer: an LLM agent that wants to edit a document, not operate a record
store. Consequences observed in real sessions:

- `list_paragraphs` returns summaries, not content, so knowing the document
  forces `N × get_paragraph` round trips before the first edit.
- Each `replace_text` is one micro-transaction (paragraph_id + old + new +
  operation_id + draft state), so a 10-place revision is 10 mutations with
  10 rounds of schema/state reasoning.
- 36 coequal tools create selection entropy: the agent must decide between
  `replace_text` / `batch_edit` / `review_apply_patch` / `commit_sync` /
  `decide_all` for every step.

The internal machinery is already file-shaped: ADR 0036 defines `edit.md` as
a generated, span-free agent projection bound to `typed.md` by
`edit.state.json` (clean/dirty/stale-clean/conflict), and the sync engine
already owns hunk alignment, style-ownership preservation, and mixed-style
policy. What is missing is an agent-facing composite layer over it — the
same inversion `byte-surgery-layer.md` made for structural ops: keep the
core, change the ergonomics.

Target sentence: **docx2typed should let an agent treat a DOCX as one
editable virtual text file, not as a database of paragraphs.**

## Solution (final design)

Three new composite tools layered over the existing strict surface — the
open, diff, and save roles are already played by `workdir_open`,
`diff_preview`, and `commit_sync`, and adding aliases for them would only
raise tool-selection entropy:

```text
workdir_open
→ document_read / document_search
→ document_patch
→ diff_preview
→ commit_sync
→ build_docx → verify_output
```

### document_read

Returns `{view, revision, state, paragraphs, content, first_id, last_id,
windowed}`. `content` is the exact virtual-file text and is the frozen
diff base: **document_read.content == the text document_patch(diff=…)
maps against**. Blocks carry their `<!--@p id="...">` markers, so a diff
generated from a windowed read locates its hunks by marker id + exact
body — line numbers in `@@` headers are never trusted. `revision` is the
opaque token (edit projection body hash) passed back as
`document_patch(base_revision=...)`.

Views: `content` (whole or `anchor`-windowed), `outline`
(one-line-per-paragraph orientation map), `auto` (default: content under
~40k chars of projection, outline above). Non-editable structure
(opaque tokens, revision gaps, locked complex tables) renders read-only.

### document_search

`document_search(query, context_chars=3000)` returns each match as the
whole enclosing block with `prev_id`/`next_id` anchors. A normal edit
should never need `get_paragraph` to locate prose; `get_paragraph` is for
post-refusal region diagnosis only.

### document_patch

One call per editing intention, two accepted forms, one atomic write:

1. **hunks**: replace / `insert_after` / `delete` dicts. A paragraph may
   carry several non-overlapping replace hunks (git-apply semantics):
   each must be unique in the pre-batch paragraph; spans are applied
   anchored by start offset in descending order. Overlapping spans are
   rejected (`document-patch-hunks-overlap`) before anything is written.
2. **unified diff** against the projection as a virtual file (full or
   windowed `document_read` content). The diff's old side is verified
   block-by-block against the current draft (`patch-context-mismatch` on
   staleness); structural changes — marker edits, header edits, block
   reorders, whole-block insertions/deletions — fail closed with
   `patch-structure-immutable` / `patch-block-inserted` /
   `patch-block-deleted`. Changed blocks are decomposed by
   SequenceMatcher into minimal non-overlapping spans; pure insertions
   are rewritten as anchored replacements around a minimal unique
   context span.

Guarantees:

- **Core decides style.** The patch gate is the sync engine's own dry-run
  (`plan_sync`) over the in-memory candidate projection — deterministic
  mixed-style mapping is accepted with warnings, ambiguous/protected
  rewrites are rejected with the engine's own codes. The facade does not
  duplicate the paragraph primitive's single-region gate.
- **base_revision rides inside the mutation transaction**, after the
  operation-id ledger: an exact retry of a completed patch replays its
  original result; a genuinely stale view fails with
  `stale-document-view` before any parsing.
- **Atomic**: all hunks are validated and applied in memory; a single
  `_write_edit` + `_refresh_regions` lands only after the Core gate
  passes.

### Progressive disclosure

- SKILL.md routes ordinary text editing through the facade
  (`DEFAULT EDITING PATH`); the six paragraph primitives are the
  advanced fallback lane (diagnosis, same-paragraph multi-region
  rewrites, recovery) — enforced in SKILL.md, composites.md Playbook C,
  capabilities.md, and the tools' own descriptions.
- Tracked revisions, comments, table structure, review collaboration,
  and recovery stay on the primitive tools — entered only when the
  document contains those structures.

## Shipped

- Phase 1: `document_read` + `document_search`.
- Phase 2: `document_patch` (hunks + unified diff, atomic).
- Ergonomics round: same-paragraph multi-hunks, `base_revision`,
  SKILL/composites/capabilities routing flip, fallback annotations in
  the primitive tool descriptions.
- Core-decided style gate, in-transaction `base_revision`, unified
  read/diff contract, structural diff fail-closed, pure insertions,
  multi-span diff decomposition, facade-first recovery mapping.

## Non-goals

- No new edit semantics: the facade adds zero validation rules and removes
  none. `typed.md` stays canonical; `edit.md` stays a governed projection.
- No Markdown-table editing of arbitrary tables in v1: normal tables project
  read-only; structure changes keep going through the table ops.
- No weakening of fail-closed behavior to make editing "feel looser".

## Implementation phases (shipped)

1. `document_read` + `document_search` — projection serving + windowed
   search.
2. `document_patch` — hunks + unified diff, one atomic write per call.
3. Ergonomics round — same-paragraph multi-hunks, `base_revision`,
   SKILL/composites/capabilities routing flip, fallback annotations in
   the primitive tool descriptions.
4. Core-decided style gate, in-transaction `base_revision`, unified
   read/diff contract, structural diff fail-closed, pure insertions,
   multi-span diff decomposition, facade-first recovery mapping.

## Verification

- Unit tests: full-read invariant (`document_read.content` == projection
  bytes), windowed read/search, multi-hunk patch byte-identical to
  sequential primitives, overlap/repeat conflict rejection, stale
  `base_revision` → `stale-document-view`, marker edit →
  `patch-structure-immutable`, windowed diff with lying `@@` numbers,
  pure insertion, multi-span diff decomposition, full-paragraph
  cross-region rewrite accepted by the Core gate.
- End-to-end: orient → locate → patch → save in four facade calls, then
  build + verify clean.

