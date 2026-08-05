# 0012 — Derive the hybrid baseline from the fingerprinted template

Build re-extracts the original typed model in memory from the fingerprint-matched template instead of duplicating the full source text in `format.json`. The sidecar keeps XML replay data, package manifest, and schema versions; incompatible typed-model or canonicalizer versions fail closed.
