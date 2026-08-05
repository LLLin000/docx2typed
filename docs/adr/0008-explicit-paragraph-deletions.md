# 0008 — Require explicit paragraph deletion tombstones

Removing a paragraph block is ambiguous and can silently lose template content. Typed mode therefore requires `<!--@delete id="P5"-->` for intentional deletion; every original paragraph ID must have either a live block or a tombstone. Unaccounted orphan records are validation errors, and section-bearing deletions are rejected until section-aware editing exists.
