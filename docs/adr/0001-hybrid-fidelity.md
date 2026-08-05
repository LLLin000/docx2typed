# 0001 — Hybrid fidelity: byte replay for untouched paragraphs, canonical synthesis for touched

v1 (strict-run-mode) promised byte-identical paragraph XML, which run-merging makes
impossible for edited paragraphs. We chose hybrid fidelity: an untouched paragraph
(md block == extract-time snapshot) is replayed verbatim from the JSON XML and verified
byte-level; a touched paragraph is re-synthesized from its spans and verified by
canonical-form comparison. verify infers per paragraph: byte-equal OR canonical-equal
passes, a differing style-ID sequence is reported as a format change (intentional or not).

The alternative "full canonical" would have surrendered byte fidelity for untouched
paragraphs; "full byte" (no run merging) would have kept v1's edit experience, which is
the problem v2 exists to solve.
