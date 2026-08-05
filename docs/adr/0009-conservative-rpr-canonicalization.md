# 0009 — Canonicalize rPr conservatively

The canonical rPr used for style IDs, run merging, and comparison removes only certain XML lexical differences through a namespace-aware parser; the original rPr remains the generation source. Typed mode v2.0 does not resolve the full Word style/theme inheritance graph, and uncertain equivalence is kept as distinct styles because extra spans are safer than an incorrect merge that changes formatting.
