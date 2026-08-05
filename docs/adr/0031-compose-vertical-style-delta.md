# 0031 — Compose vertical normalization with existing character formatting

Converting a Unicode vertical character preserves its enclosing run's rPr and adds or replaces only the vertical-align property. Bold, font, color, language, and other properties remain; an existing conflicting `vertAlign` or `position` is manual/error. The resulting complete rPr receives its own content-addressed style ID after re-extraction.
