# 0011 — Store each typed paragraph on one logical line

A file newline is not a Word line break. Each typed paragraph uses one logical source line; actual Word breaks use an explicit `docx-inline kind="br"` token, while view projections may wrap for display. This keeps parsing and diffs deterministic and preserves whitespace semantics.
