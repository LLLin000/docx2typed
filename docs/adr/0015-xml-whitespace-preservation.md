# 0015 — Preserve typed whitespace and derive xml:space at generation

The typed AST never trims text. The builder computes `xml:space="preserve"` for `w:t` values with leading or trailing ordinary spaces; tabs and Word line breaks are explicit inline tokens. This prevents text-only edits from silently changing spaces while keeping whitespace metadata out of the AI editing surface.
