---
status: accepted
---

# 0037 — Revision support: visibility, tracked editing, and decisions

Revision-bearing documents (Word Track Changes) are currently unusable: the
extractor opaque-ifies every `w:ins`/`w:del` container, discarding the revised
text (insertions and `w:delText`), locking the affected paragraph against
editing, and — because comment/bookmark anchors can sit inside revision
containers — breaking anchor pairing so the whole workdir fails validation.
Revision support is built in three slices: R1 visibility and preservation,
R2 tracked run-text editing, R3 revision decisions.

The typed AST gains `RevisionNode` as a first-class recursive container
(`kind` in `insert`/`delete`/`move_from`/`move_to`, carrying `ooxml_id`,
`author`, `date`, optional `date_utc`, `attrs`, and `children`). `TextNode`
stays neutral: rendering chooses `w:t` or `w:delText` from the ancestor
context, so rejecting a deletion is just unwrapping the node. The agent
editing surface (`edit.md`) is the final view (Word's No Markup): inserted
text visible, deleted text hidden but its position preserved as an
uneditable zero-width `⟦revision-gap id="R…" kind="delete"⟧` placeholder, so
insertions around a deletion have deterministic positions and sync cannot
drift the hidden revision. Deleted text is locked in v1 (readable via
`revisions.md`, never editable through text tools).

Tracked editing in R2 follows one uniform mapping — insert → new
`RevisionNode(insert)`, delete → new `RevisionNode(delete)`, replace →
`delete` + `insert` — but AST mutation must preserve and understand revision
ancestry (editing text inside an existing insertion nests the new
`del`/`ins` inside the outer revision; the exact Word-produced shape is a
project definition pending a real Word fixture, not a fidelity claim).
Paragraph-level insert/delete tools are rejected in track mode
(`track-paragraph-revision-not-supported`) until R2.5 models paragraph-mark
revisions. Editing revision text in direct/no-track mode is rejected with
`revision-text-mutated-in-direct-mode`.

Edit mode is a three-field state, never inferred from one signal:
`source_track_enabled` (settings.xml `w:trackChanges`), `has_pending_revisions`,
and `effective_edit_mode` (`track`/`direct`/`ambiguous`). Extraction always
succeeds; `ambiguous` (pending revisions without `trackChanges`, or vice
versa) blocks revision-generating calls until the user explicitly chooses
`--track`/`--no-track` — an audit document is never silently edited in place.

New revision identity: session `author` (explicit workdir_open parameter →
project config → `DOCX2TYPED_AUTHOR` → fallback plus warning, with the
`author_source` recorded in run evidence); `w:date` always written, the
Microsoft-365-only `w16du:dateUtc` written only when the source already uses
it or the target profile supports it (namespace and `mc:Ignorable` must be
maintained); `w:id` allocated as the lowest available non-negative integer
over a package-wide scan of all WordprocessingML parts (document, headers,
footers, footnotes, endnotes, comments). Tool-level identity uses a stable
`revision_key` (part + container path + kind + `w:id` + content fingerprint),
never `w:id` alone.

Verification gains three layers: final-text signature (normal + insert +
move_to, excluding delete + move_from), original-text signature (normal +
delete + move_from, excluding insert + move_to), and revision-structure
signature (kind, key, id, author, date, attrs, parent path, children,
styles). Independent verify requires all three to match plus package
integrity; this is a hard acceptance gate for R1.

Safety boundaries: paragraphs containing `w:rPrChange` are extracted,
viewable, and byte-replayed but locked against editing in R2 (the style
canonicalizer must not leak old run properties into inheritable styles);
unsupported revision content (fields, math, drawings, content controls,
custom XML, move/conflict revisions) keeps its raw XML, appears in the
revision inventory as non-editable, and the paragraph replays untouched;
only direct-body run insertions/deletions enter the structured editing
surface, while the package-wide revision inventory marks everything else
`editable: false` with a reason. `accept`/`reject` on a single revision uses
`revision_key` + `expected_fingerprint` and reuses the sync transactional
publish; accept-all/reject-all produce a new clean baseline project
(re-extract), matching the normalization governance pattern. This decision
supersedes the opaque-ification of `w:ins`/`w:del` containers and extends
the verify contract; it does not relax the editable-surface, opaque-node, or
package-integrity contracts elsewhere.
