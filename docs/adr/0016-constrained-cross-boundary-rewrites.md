# 0016 — Reject cross-boundary text rewrites in v2.0

Text-only typed mode permits edits within existing text nodes and preserves the structure skeleton. It does not infer how a replacement crossing style spans, ranges, or anchors should be redistributed; the validator rejects such edits instead of guessing a style or silently changing formatting. A visible-text AST rewrite operation can be added later from real failure cases.
