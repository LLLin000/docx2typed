# 0023 — Fail on source/template drift instead of auto-merging

If the recorded source or workdir template fingerprint changes after extract, the current workdir is stale. v2.0 refuses build and does not overwrite or three-way-merge pending typed edits; the user creates a new workdir from the latest DOCX and reapplies unfinished changes explicitly.
