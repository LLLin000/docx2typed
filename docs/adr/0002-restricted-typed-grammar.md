# 0002 — Use a restricted typed grammar instead of generic Markdown/HTML parsing

`typed.md` keeps Markdown-like readability but is not a CommonMark document. docx2typed owns a narrow parser for paragraph directives, approved spans, structural tokens, and approved range containers; ordinary text is preserved literally. Generic Markdown/HTML parsing was rejected because escaping and HTML normalization can change patent/formula text, and HTML5 does not reliably preserve self-closing custom elements. Clean/style/raw views must use the same typed parser.
