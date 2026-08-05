# 0013 — Patch only direct-body paragraph byte ranges

Typed build must copy the template's original `word/document.xml` bytes and replace only explicitly located direct-body paragraph slices. Untouched paragraphs, tables, section properties, and other XML bytes remain unchanged; touched paragraphs are generated from the validated typed AST. Whole-document serializers, including `python-docx.save()`, cannot provide this boundary guarantee.
