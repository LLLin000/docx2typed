# 0005 — Lock the typed structure skeleton while allowing text-driven normalization

Existing typed paragraphs keep their style IDs, structural tokens, range attributes, and order immutable. Text edits may remove empty spans and merge adjacent equivalent styles because those are consequences of deleting or joining text, not format-authoring operations. Build validates this skeleton before writing any DOCX and rejects other markup mutations.
