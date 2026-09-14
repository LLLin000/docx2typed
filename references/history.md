# History reference

Load this file for savepoints, restore, historical export, baseline
transitions, or retention. The Version commit graph is the history authority;
`history-trim.jsonl` is only the retention audit ledger.

## One workspace

A trusted workdir belongs to one logical DOCX. Resume it across editing
requests. Create another workdir only for a first import, an explicit fork, or
a different source document.

```text
edit.md draft
    │ document_patch / document_replace
    ▼
canonical typed state
    │ commit_sync (ordinary save boundary)
    ▼
Version Vn → Tree Tn → content-addressed maps/blobs
    │
    └── workdir.json HEAD points to the current Version/tree
```

The store's `generations/` are transaction and recovery materialisations, not
user history. A Version is immutable. Its parent chain is linear history.

## Save semantics

```text
draft_dirty=false, version_dirty=false, publish_pending=false → no-op
draft_dirty=false, version_dirty=true                    → new Version
draft_dirty=true                                         → sync, compare tree, then decide
publish_pending=true without canonical drift             → publish; no Version
```

Ordinary draft edits become history only at `commit_sync`. Restore, baseline
settlement, and table transitions are separately governed Version-producing
operations. An explicit `label` describes a Version; `pin=true` independently
makes it a retention root.

## Reading history per paragraph

`history_list` names the Versions; two readers answer the questions a
paragraph-anchored document actually asks:

```text
history_diff(Vn)        → which paragraphs Vn added / changed / removed
                          (default: against its parent; previews included)
history_blame("P39")    → which Version last changed P39, with that
                          paragraph's before/after preview
```

The unit of history is the paragraph, not the save: a batched save stays one
Version while remaining readable one paragraph at a time. Find what a Version
touched with `history_diff`, find a paragraph's origin with `history_blame`,
then take exactly that paragraph back with
`history_restore(version, paragraphs=["P39"])` — the guards are the ones under
"Selective restore v1" below. Both readers are derived from the object graph
(commit chain, trees, content-addressed paragraph blocks), so reading history
never rewrites it.

## Restore and export

`history_restore(Vn)` is a forward snapshot restore:

```text
V1 ← V2 ← V3 ← HEAD
              │ restore V1
              ▼
V1 ← V2 ← V3 ← V4( tree=T1 ) ← HEAD
```

It never rewinds or deletes the old chain. The closest Git operation is
`git restore --source V1 -- .` followed by `git commit`, not `git revert`.
A selective restore is the same source-restore idea applied to paragraph paths,
with a new Version after guards pass.

`build_docx(version="Vn")` exports a historical tree without moving HEAD.
A current export requires a clean, committed state.

## Selective restore v1

Only dependency-free, zero-token paragraphs are eligible, with style IDs
present in the current registry. Revision/comment/bookmark/range anchors,
SDTs/content controls, and table or text-box topology dependencies refuse with
`partial-restore-needs-dependent-state`; there is no silent whole-restore
fallback. Use an explicit patch or whole restore instead.

## Retention

`history_gc(keep_last=N)` keeps the newest `N` Versions and every explicitly
pinned Version. Commit metadata remains visible. Trimmed content is recorded in
`history-trim.jsonl`, and the trim transaction progresses through durable
`intent → prepared → retention-marked → retention-swept → completed` phases.

A trimmed Version remains in `history_list` as `content: trimmed`; verify treats
that intentional loss as healthy, while restore and historical export fail
closed with `version-trimmed`. Recovery finishes the recorded decision instead
of inferring state from timestamps or resurrecting content from a generation.
