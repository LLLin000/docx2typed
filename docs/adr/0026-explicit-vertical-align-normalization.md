# 0026 — Make vertical-align conversion an explicit pre-extract normalization

Normal typed extraction preserves Unicode and Word-style superscript/subscript representations and exposes the distinction in Style view. An explicit normalization profile may convert a whitelisted set using `preserve`, `all`, or selective policy, but it must emit a new normalized DOCX baseline and be followed by a fresh extract; ordinary text-only build never performs the conversion implicitly.
