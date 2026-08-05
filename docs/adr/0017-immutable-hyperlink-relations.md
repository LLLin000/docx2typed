# 0017 — Keep hyperlink relations immutable while allowing link-text edits

An existing hyperlink range may contain editable visible text, but its relationship ID, internal anchor, target, and relation attributes are locked. v2.0 does not create or retarget hyperlinks; the range skeleton and template relationships remain unchanged.
