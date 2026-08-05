# 0032 — Scan and normalize vertical candidates in the typed AST

Candidate discovery and conversion operate on the typed AST, not a second raw-XML scanner. This preserves paragraph, style, range, and opaque context; the resulting normalized model is then emitted through the same byte-preserving DOCX patch path and re-extracted.
