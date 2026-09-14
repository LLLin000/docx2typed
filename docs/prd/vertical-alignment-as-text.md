# PRD: Vertical alignment is text (superseding the span-only representation)

Status: draft · 2026-09-14 · branch `feature/vertical-align-as-text`

## Problem Statement

Superscript and subscript are represented **twice** today, and the two
representations disagree:

| | input | storage (`typed.md`) | read (projection) | built package |
|---|---|---|---|---|
| `Ca2+` marked superscript | `Ca^{2+}` accepted | `<span data-s="s_…">2+</span>` | **plain `Ca2+` — invisible** | `<w:vertAlign w:val="superscript"/>` ✅ |

`^{…}` / `_{…}` is therefore an *input* grammar only: the engine can be told
about vertical alignment, but reading a document does not show it. The cost is
observable in a real session: asked to check and repair the superscripts of a
patent, the agent wrote its own DOCX-parsing audit scripts (36 shell calls, 5
generated scripts, two rounds of fixing the scripts themselves) because the
text it could read did not contain the fact it had to inspect. Editing shows
the same asymmetry: because the formatting lives in a span beside the text,
moving or replacing that text drops it.

## Decision

**Vertical alignment becomes text — in the projection *and* in the canonical
typed source — whenever the region differs from its paragraph's base style by
`vertAlign` alone.**

```
typed.md / projection:   …浸渍于Ca^{2+}/Cu^{2+}双金属离子浴中…
build:                   <w:r><w:rPr><w:rFonts …/><w:vertAlign w:val="superscript"/></w:rPr><w:t>2+</w:t></w:r>
```

Region classes:

| region vs paragraph base style | representation |
|---|---|
| differs by `vertAlign` only | text: `^{…}` / `_{…}` (escape: `\^{…}` for a literal) |
| differs by anything else as well | span, unchanged — and *reported* by whatever audit surface exists |
| deleted-revision content | unchanged (history stays a span) |

The build mapping already exists and is verified: a `^{2+}` in the source
produces `<w:vertAlign w:val="superscript"/>` in the package (raw-run dump,
2026-09-14). The escape rules (`\^{`) and the vertical-variant synthesis are
also already implemented — this PRD moves the *reading* direction to match.

Measured on the real patent workdir that motivated this:

```
55 regions  differ by vertAlign alone        → become ^{…} / _{…}
 4 regions  also carry font:hint=eastAsia   → keep the span, report it
 0 regions  differ by a font change alone   → (the fonts are the body style's own)
```

## Why text and not a formatting query tool

- **reading is lossless**: what the agent reads is what it edits;
- **detection is a regex** over the projection (`\^\{…\}`), so "find every
  superscript" needs no new tool;
- **editing carries the fact with the text**: replace/move a run and its
  superscript travels, instead of being stranded in a span;
- **batch repair is an existing tool**: `document_replace(regex=true,
  find="([A-Za-z])(\\d+)([+-])", replace="\\1^{\\2}\\3",
  expected_matches=N, dry_run=true)` — no `format` payload, no `format_audit`;
- **the build direction is already implemented and verified**, so this is one
  direction of one feature, not a new subsystem.

## Non-goals

- Bold / italic / size / font / colour stay spans: they do not change the
  glyph sequence, and the text layer is already dense with revision, comment
  and token syntax.
- `^{…}` does not carry attributes. A region whose vertical alignment comes
  with a different font stays a span (4 such regions exist in the measured
  document); making the tag carry properties would turn the text into a
  property dump.
- No auto-repair: an audit reports (missing / over-marked / not text-ifiable),
  and a repair is an explicit call.

## Migration

Existing workdirs store vertical alignment as spans. The promotion is
deterministic (delta against the paragraph base style, as above), so it can run
during `extract`/`refresh`; `styles.json` keeps every style — the ones that
become text simply stop being referenced by a span unless another property
needs them. `verify` is per generation, so a promoted workdir verifies against
its own baseline; workdirs are not silently re-baselined.

Open: whether an existing workdir is promoted **in place on next refresh** or
through **a version/baseline transition** (the second is louder, the first is
what "I just want to keep working" asks for).

## Open decisions

1. Regions that carry `vertAlign` **plus** another difference (e.g.
   `font:hint=eastAsia`): keep the span and report it (recommended — 4 regions
   in the measured document), or normalise them to text and accept the loss?
2. Migration: promote in place on refresh (recommended) or take a baseline
   transition per workdir?

## Evidence and follow-ups

- verified 2026-09-14: `^{2+}` → `<w:vertAlign w:val="superscript"/>` in the
  built package (raw run dump), so the build direction is not a claim;
- the projection/storage change needs the promotion pass plus a `verify`
  round-trip test over a document with span-based superscripts;
- the audit surface (if any) stays a *report* — the measured session shows the
  missing piece was readability, not a missing detector.
