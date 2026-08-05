# 0010 — Escape literal text with XML entities in typed markup

Typed markup uses XML entities for literal `&`, `<`, and `>`; the restricted parser decodes them only in text nodes and rejects unknown entities/tags. Markdown punctuation remains literal and Unicode whitespace is preserved. This makes ordinary patent text unambiguous without adding a second backslash-escape language.
