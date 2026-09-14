# PRD: Vertical alignment is text (canonical representation, schema 2)

Status: **contract** · 2026-09-14 · branch `feature/vertical-align-as-text`

Design conclusion (frozen):

> **Superscript/subscript is a first-class textual annotation of editable
> character content, but only when the engine can factor `w:vertAlign` out of
> the run formatting reversibly and losslessly. All other run properties remain
> style state; history and structural content are never promoted.**

## Problem Statement

Superscript is represented twice today, and the two representations disagree:
`^{…}` is accepted on input, storage keeps a span, the projection shows plain
text, and only the built package carries the real `w:vertAlign`. Reading
therefore cannot see what editing must preserve — measured in a real session
where an agent wrote its own DOCX-parsing audit (36 shell calls, 5 generated
scripts, two rounds of fixing the scripts) to check superscripts. And because
the fact lives in a span beside the text, replacing or moving that text drops
it.

## Eligibility: canonical identity, not a feature subset

A region is textifiable **iff** removing exactly one `w:vertAlign` from its run
properties yields run properties canonically equal to its paragraph's base
style:

```
canonical(region.rPr − exactly_one(w:vertAlign)) == canonical(base_style.rPr)
```

NOT `rpr_features(region) − rpr_features(base) == {"vertAlign"}`: `rpr_features`
enumerates a subset of Word's `rPr` vocabulary (booleans, `rFonts`, and a fixed
set that includes `vertAlign`), while `StyleRegistry` stores the full canonical
`rPr`. A feature-dict diff could silently ignore an unenumerated property.

**One predicate, two directions.** `vertical_style_variant()` already refuses
`w:position` and a conflicting `w:vertAlign` when *writing*. Reading/promotion
must call the same eligibility function — e.g.
`factor_vertical_style(style, underlying_style) -> textifiable | preserve-span`
— so extract and write can never disagree about what may become text.

## Boundaries: what becomes text, and what never does

| Becomes text | Never becomes text |
|---|---|
| `w:vertAlign w:val="superscript"` → `^{…}` | `w:position` — not vertical-alignment semantics |
| `w:vertAlign w:val="subscript"` → `_{…}` | Unicode glyphs `² ³ ⁺ ⁻` — real characters, not Word formatting |
| | OMML equations — structural objects |
| | content inside `deleted` / `moveFrom` history — history is never rewritten |
| | `rPrChange` format history — a revision, not a property |
| | fields and opaque structural runs |

`²` and `^{2}` are permanently different things: the first changes the code
point, the second keeps the character `"2"` with Word vertical alignment. **No
automatic conversion in either direction.**

Measured on the patent workdir that motivated this: 55 regions qualify, 4 also
carry `font:hint=eastAsia` (keep the span, zero property loss), 0 differ by a
font alone. The 4 are not normalised in v1 — the future direction is *style
factorization* (`<span data-s="S_font_hint">^{2+}</span>`), not ignoring the
attribute.

## Writer funnel: one canonical state

The canonical state is only ever `^{…}` / `_{…}`. Every writer lowers into it:

| call | canonical result |
|---|---|
| `document_patch` with `Ca^{2+}` | `Ca^{2+}` in the projection and the typed source |
| `format_span(…, {"vertAlign": "superscript"})` | lowered to `^{…}` (API kept for compatibility) |
| `format_span(…, {"vertAlign": "subscript"})` | lowered to `_{…}` |
| `format_span(…, {"vertAlign": "baseline"})` | removes the vertical marker — a real removal, not a no-op |
| bold / italic / size / font | unchanged: the style lane |

Without this, the double representation would simply move into two write paths.

## The diff unit must treat vertical as orthogonal

Today a vertical tag yields a unit `(("V", char, "superscript"))` while ordinary
text yields `(("X", char))`, and the original style ownership is then re-applied
via `vertical_style_variant()`. If the baseline unit stays
`text="2+" style=S_superscript`, then **deleting `^{…}` is a false delete**: the
style assignment re-inherits `S_superscript` and the built document is still
superscript even though the tag is gone.

The model becomes:

```
logical style ownership = S_base        (not S_superscript)
vertical semantic       = superscript   (a dimension of its own)
```

and these four operations must be genuinely symmetric:

```
Ca2+     → Ca^{2+}   add
Ca^{2+}  → Ca2+      remove        (the false-delete hazard)
Ca^{2+}  → Ca_{2+}   switch
Ca^{2+}  → Ca^{3+}   edit content, keep vertical
```

## Migration is a representation migration, not a baseline transition

The DOCX does not change: not one bit. So promotion must not produce a User
Version, must not bump `baseline_epoch`, and must not set `version_dirty`.

The typed source gains a schema that says what it means:

```
new typed source  schema=2      ( ^{…}/_{…} are canonical)
old engines       refuse it explicitly (the parser already fails closed on an
                                    unsupported schema — "incompatible typed source schema")
new engine        schema1 workdir → deterministic promotion → schema2 generation
                  head_version unchanged · baseline_epoch unchanged · version_dirty=false
                  + one migration evidence record
```

Historical versions stay immutable schema-1 objects; restoring an old version
materialises schema 1 and the current engine promotes the *working* generation
lazily. History is never rewritten.

Decision taken: **promote in place on refresh** (not a baseline transition), and
**never lose the 4 hinted regions** (they keep their span).

## Search and replace semantics

Two layers, so that neither ergonomics nor precision degrades:

```
document_read                     → annotated projection: Ca^{2+}
document_search (locator)         → matches semantic visible text ("Ca2+" finds
                                    Ca^{2+}), returns matched_text="Ca^{2+}"
                                    and a match_ref into the annotated projection
document_replace(regex=true)      → regex runs on the annotated projection
```

Mutations must never silently match against a de-marked copy of the text and
then lose the vertical alignment.

## Audit surface: `document_read(view="issues")`, no new tool

Classification is added to the existing issues view:

```
vertical.missing-candidate     ’Ca2+’-shaped text that is not marked
vertical.textified             already canonical
vertical.non-textifiable       carries an extra property (the 4 regions)
vertical.unicode-glyph         the character is ² ⁺ … — a character, not formatting
```

"Check every superscript in this patent" is then
`document_read(view="issues")` + `document_replace(…, dry_run=true)`, with no
new MCP tool. Candidate judgement ("should this `2+` be superscript?") stays in
the skill/domain layer: `Version2+`, `Model3+`, `A2+` are not chemistry.

## Interaction with the insertion chain (#91)

A same-author pending insertion must absorb the whole sequence as ordinary
content editing — no `rPrChange`, no nested revision, no refusal:

```
same author:  "加入 Ca2+"  →  "加入 Ca^{2+}"        absorb
other author:                                        refused (ADR 0047)
```

## Acceptance matrix (frozen)

| scenario | expected |
|---|---|
| native superscript in the source | `^{…}` after extract |
| native subscript in the source | `_{…}` after extract |
| plain → superscript | tag added; real `w:vertAlign` in the package |
| superscript → plain | `w:vertAlign` genuinely removed |
| superscript → subscript | switched correctly |
| edit text inside the tag | vertical preserved |
| literal `^{x}` / `_{x}` in content | auto-escaped; still literal text in the DOCX |
| `vertAlign` + `font:hint` | span kept, zero property loss |
| `w:position` | not textified |
| Unicode `²⁺` | stays Unicode, never converted |
| deleted revision content | not textified |
| pending insertion, same author | absorbed normally |
| pending insertion, other author | still refused |
| no-op migration | **DOCX byte-identical** |
| schema1 → schema2 | no User Version, no baseline epoch bump, not version-dirty |
| historical restore | old Version restores; the current engine projects it |
| `format_span(vertAlign=…)` | canonical state is still `^{}` / `_{}` |
| body / table cell / header / footer / note / textbox / SDT | same projection contract |

## Non-goals

- Textifying bold / italic / size / font / colour (they do not change the glyph
  sequence, and the text layer is already dense with revision, comment and
  token syntax).
- Attributes inside `^{…}`.
- Auto-repair of anything: audits report, repairs are explicit calls.
- Deciding domain semantics ("should this be superscript?").
