---
status: accepted
---

# 0036 — Hash-bound clean edit projection and governed synchronization

`typed.md` remains the canonical typed AST serialization; `edit.md` is a generated, span-free Agent projection and patch input. Clean text changes may cross typed style spans only through `edit sync`, which validates protected structure, aligns Unicode grapheme/token units, preserves unchanged ownership, and assigns new text under the explicit Word-like policy; direct raw `typed.md` edits keep the conservative rejection rule from ADR 0016. The projection header binds `base_typed_sha256`, `base_projection_sha256`, schema/segmentation versions, and the sync contract, yielding `clean`, `dirty`, `stale-clean`, and `conflict` states; build fails closed outside `clean`.
`base-typed-sha256` hashes the exact canonical `typed.md` bytes; `base-projection-sha256` hashes the canonical projection body with the header excluded and declared line-ending normalization applied, so the freshness check is not self-referential or platform-dependent.
The synchronizer records occurrence- and hunk-level provenance and keeps the style registry, package structure, and opaque paragraphs immutable. Mixed-style replacement is rejected by default and is accepted only for an anchored, uniquely aligned local hunk with an explicit warning; an unanchored mixed full-paragraph rewrite fails. Sync stages complete outputs and uses recovery-by-stale-projection if the second flat-file replacement is interrupted, rather than claiming two independent replacements are one filesystem transaction. This decision supersedes ADR 0016 only for the governed clean-sync seam and does not add a second canonical source.
