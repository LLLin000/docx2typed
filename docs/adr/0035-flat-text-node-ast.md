# 0035 — Use flat styled Text nodes in the typed AST

The typed AST represents effective character formatting on `Text(style_id, text)` nodes; `<span data-s>` is only the Markdown-like serialization projection. Anchors, inline tokens, ranges, and opaque nodes remain structural AST nodes. This removes empty/nested span ambiguity and lets parser, validator, normalizer, builder, and verifier share one model.
