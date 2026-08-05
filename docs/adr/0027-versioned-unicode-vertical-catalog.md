# 0027 — Use a versioned Unicode vertical catalog for conversion

Vertical normalization uses a pinned Unicode-data catalog rather than a short hand list. It covers compatibility `<super>`/`<sub>` mappings across digits, signs, operators, delimiters, letters, modifiers, and ordinals, with explicit manual/ambiguous entries for named vertical characters without simple decompositions. `all` converts cataloged mappings only; anything outside the catalog is preserved or rejected by policy, never guessed.
