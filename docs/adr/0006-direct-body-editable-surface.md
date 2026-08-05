# 0006 — Limit typed editing to direct body paragraphs in v2.0

The editable surface is the direct `w:body` `w:p` sequence only. Tables, text boxes, headers/footers, footnotes, and other nested containers remain opaque template content and are not represented as typed paragraphs. This avoids flattening nested paragraphs into the body; a later hierarchical AST can add container editing without weakening the v2.0 integrity contract.
