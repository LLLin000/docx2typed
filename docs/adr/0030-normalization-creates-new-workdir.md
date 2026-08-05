# 0030 — Normalization creates a new workdir

Vertical normalization never edits the source DOCX or current typed workdir in place. After candidate review and a complete policy, it emits a new normalized DOCX/workdir with a new template fingerprint, fresh extraction, policy, and audit; the original workdir remains reproducible and untouched.
