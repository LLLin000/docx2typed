# 0025 — Keep Unicode and Word vertAlign representations independent

Unicode superscript/subscript code points remain literal text, while ordinary characters with `w:vertAlign` remain style-span formatting. Typed mode never converts, folds, or compares these representations as equivalent; fixtures must cover both forms so canonicalization cannot erase the distinction.
