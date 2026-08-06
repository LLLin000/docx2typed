# PRD: Comment decisions (accept-all clears comments, single comment delete)

> **Status:** implemented (commit `13300f7`). Source: goal-mode gap analysis
> 2026-08-06 — "对标 Word 的 DOCX 编辑体验：补齐批注决策". ADR 0018
> (immutable comment/bookmark anchors), 0037 (revisions), 0038 (containers)
> govern.

## Problem Statement

`decide accept-all` settles tracked revisions but leaves comments untouched.
Word's native "Delete All Comments" and per-comment delete have no CLI/MCP
equivalent, so users must hand-edit comments.xml or open Word. Two surfaces
are missing:

1. **Batch decision.** `accept-all`/`reject-all` should clear every comment
   (matching Word's "Delete All Comments in Document").
2. **Per-comment decision.** A single comment should be deletable by its
   comment id without touching the rest of the document.

Comments are the only editable part whose anchors (`w:commentRangeStart` /
`w:commentRangeEnd` / `w:commentReference`) live in document.xml while the
content lives in a separate part. Deleting a comment therefore requires
coordinated edits across two parts plus re-anchoring of survivors.

## Solution

- **comments.xml becomes an editable part.** `PART_KEYS_PATTERN` extended;
  `locate_part_xml` accepts a `comments` root with `comment` entries and
  returns `entry_ranges` (all call sites upgraded to the 7-tuple). Entries are
  addressed by their `w:id` (`part_entry_id` in format.json).
- **accept-all/reject-all clear comments.** After revision settlement,
  `clear_comments_from_document` strips every `commentRangeStart`/`commentRangeEnd`/
  `commentReference` element from document.xml (byte-level, anchored like other
  strips) and `empty_comments_part` rewrites comments.xml to a valid empty root
  (original root tag preserved, paired open/close, XML-declaration regex bug
  fixed via `re.search`).
- **Single comment delete.** `decide comment-delete <id>` / MCP
  `delete_comment()`: removes the comment entry paragraph(s) from comments.xml
  (located via format.json, not typed.md), writes a tombstone (empty root is
  validated; tombstones are allowed in validate), strips the anchors
  recursively (`_strip_comment_anchors` / `_contains_comment_anchor`), records
  `anchored_paragraphs` as changed_ids, and runs `merge_adjacent_text` so the
  serialized XML matches in-memory state. Unknown id → stable
  `comment-not-found` error.
- **Part rendering covers all template parts.** `_render_parts` renders every
  template part so an emptied comments part survives build even with zero live
  paragraphs; the locator handles a self-closing empty root round-trip.
- **Namespace hygiene.** `_canonical_ppr` uses the full `nsmap` including
  `NS_W16DU` (fixes unbound `w15` prefix on comment pPr); `annotationRef`
  added to known inline markers.

## Acceptance

- `accept-all` output: comments.xml empty, no comment anchor elements remain
  in document.xml, verify green.
- `decide comment-delete <id>`: only that comment removed; other comments
  re-anchored correctly; verify green; `comment-not-found` for unknown ids.
- CLI + MCP both expose the operations; tool smoke covers the happy path and
  the error path.

## Out of scope

- Comment editing (text changes inside an existing comment) — not requested.
- Reply chains / resolved-state (Word stores resolution in `w:comment` attributes;
  deletion removes the whole comment).
