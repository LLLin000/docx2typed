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

## Solution

Add 6 composite tools ("Agent Editor API") on top of the existing surface.
The 36 primitive tools remain unchanged as the advanced/recovery lane; the
facade never bypasses their validation, it only batches and hides it.

```text
document_open      → wraps workdir_open (same negotiation, same envelope)
document_read      → whole or windowed edit.md projection with block markers
document_search    → context-windowed full-text matches over the projection
document_patch     → one multi-hunk patch (unified diff or hunk list)
document_diff      → wraps diff_preview, returns projection-level diff
document_save      → composite: diff_preview + preflight + commit_sync evidence
```

Agent-visible flow for a typical task drops from ~12 RPCs to 4:

```text
document_open → document_read(search) → document_patch → document_save
```

### document_read

Returns the `edit.md` projection verbatim, with stable block markers so a
patch can anchor without prior `get_paragraph` calls:

```markdown
<!-- P0 -->
研究背景

<!-- P1 -->
肩袖再撕裂是肩袖修复术后常见并发症……
```

- Small documents: `document_read()` returns the whole projection.
- Large documents: `document_read(anchor="P47", before=5, after=8)` returns
  a block window; `document_read(view="outline")` returns headings/first
  lines for orientation.
- Non-editable structure (opaque tokens, revision gaps, locked complex
  tables) renders as its existing typed-grammar placeholder — visible,
  read-only, fail-closed if a patch touches it. The projection never
  flattens what the validator would reject anyway.
- Every response carries the projection generation / `edit.state.json`
  state so the agent can detect staleness.

### document_search

`document_search(query, context_chars=3000)` returns each match as a full
context block spanning the enclosing paragraph markers — not a hit list of
IDs. This replaces the locate-by-get_paragraph loop.

### document_patch

One call, many hunks. Two accepted forms:

1. Hunk list: `{base_state, hunks: [{paragraph_id?, old, new, insert_after?, delete?}]}`.
2. Unified diff against the projection as a virtual `edit.md` file — the
   format coding agents already produce.

Server-side pipeline (all existing machinery, one call):

```text
parse diff/hunks
→ apply to in-memory projection copy
→ align hunks to paragraph/range coordinates   (edit_sync alignment)
→ fingerprint + overlap + style-ownership checks per touched region
→ one operation_id for the whole batch
→ draft mutation  → new state + affected paragraph IDs returned
```

Cross-boundary rewrites follow ADR 0036 exactly: mixed-style replacement is
rejected except for anchored, uniquely aligned hunks with a warning. The
facade changes how many edits arrive per call, not what is allowed.

Draft-vs-strict boundary: per-hunk structural validation stays fail-closed
(it is load-bearing — ADR 0016/0036). What the facade removes is the
per-call ceremony, not the per-edit guarantee: one `document_patch` performs
the same checks ten `replace_text` calls would, with one result instead of
ten. Strict CAS/fingerprint/generation settlement remains entirely at
`document_save` / `commit_sync`.

### document_save

Composite boundary: runs the preflight gate, `diff_preview`, and
`commit_sync`, returning one envelope with the diff summary, evidence, and
new snapshot. Agents stop orchestrating preflight → diff → commit by hand;
recovery paths still surface the same diagnostics.

### Progressive disclosure

- SKILL.md routes ordinary text editing through the 6 facade tools.
- Tracked revisions, comments, table structure, review collaboration, and
  recovery stay on the primitive tools — entered only when the facade
  reports the document contains those structures (revision gaps, comment
  anchors, locked tables).

## Non-goals

- No new edit semantics: the facade adds zero validation rules and removes
  none. `typed.md` stays canonical; `edit.md` stays a governed projection.
- No Markdown-table editing of arbitrary tables in v1: normal tables project
  read-only; structure changes keep going through the table ops.
- No weakening of fail-closed behavior to make editing "feel looser".

## Implementation phases

1. `document_read` + `document_search` — projection serving + windowed
   search over `_read_edit`/`_paragraph_blocks` in `mcp_server.py`. No new
   mutation code.
2. `document_patch` — hunk-list form first (direct mapping onto
   `_apply_patch_to_draft` / `_apply_batch_to_body`), unified-diff form
   second (parse → projection edit → same path). One `operation_id` per
   call.
3. `document_save` — composite over `_commit_sync_impl` + preflight.
4. `document_open` / `document_diff` — thin wrappers; ship last, cheapest.
5. SKILL.md + `capabilities/task_map.json` + protocol schema bundle: register
   the 6 tools in the engine descriptor, mark primitives as advanced-lane,
   update the agent workflow diagram.

## Verification

- Each tool: unit tests against the fixture corpus (small doc full-read,
  100+ paragraph windowed read/search, multi-hunk patch across touched and
  untouched paragraphs, locked-region patch → fail-closed, stale projection
  → reject).
- Regression: existing suite must pass unchanged; facade tests assert that a
  `document_patch` batch produces byte-identical draft state to the
  equivalent sequence of primitive calls.
- End-to-end: one scripted corpus scenario that reads a Discussion section,
  patches it in ≤ 4 facade calls, and builds + verifies clean.

## Resolution (2026-09-06)

Phases 1–2 shipped as `document_read`, `document_search`, `document_patch`.
Phases 3–4 resolved by inspection: `commit_sync` already is the composite
strict boundary (agent preflight gate + sync + CAS snapshot publish + one
evidence payload in a single call), `diff_preview` already is the diff, and
`workdir_open` already is the open. Adding `document_save` /
`document_open` / `document_diff` as aliases would add tool-selection
entropy without capability — the exact failure mode this PRD exists to
remove. The facade is therefore 3 new tools layered over the existing
strict surface: `workdir_open → document_read / document_search →
document_patch → diff_preview → commit_sync`.
