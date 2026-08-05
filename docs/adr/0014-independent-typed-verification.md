# 0014 — Verify the typed-to-DOCX path independently

Typed verify consumes the workdir and output DOCX, re-derives the baseline from the fingerprinted template, parses the typed source, and independently checks output text, structure skeleton, package parts, and opaque containers. A two-DOCX comparison or build-only check cannot prove that the output implements the current typed source.
