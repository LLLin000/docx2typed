# 0021 — Treat an extract as one typed workdir project

The typed workflow is directory-based: typed.md, format.json, styles.json, `_template.docx`, and their manifest are one extract. `build`, `validate`, `view`, and `verify` accept the workdir rather than independently paired sidecar paths, preventing accidental cross-document combinations.
