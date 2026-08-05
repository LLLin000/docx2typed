---
name: docx2typed
description: >
  DOCX typed-mode editing with locked formatting and structure. Trigger when
  text must change while Word formatting, comments, hyperlinks, annotations,
  package parts, and XML outside edited paragraphs must remain safe.
---

# docx2typed — typed-mode DOCX text editing

Typed mode is the only format on this experiment branch. It exposes continuous prose plus explicit, validated structural tags instead of Word run-number lines.

## Run

The package is normally loaded from its parent directory:

```bash
cd ~/.omp/agent/skills
python -m docx2typed <command>
```

Commands:

```bash
python -m docx2typed extract <input.docx> -o <workdir>
python -m docx2typed view <workdir> --mode clean
python -m docx2typed view <workdir> --mode style
python -m docx2typed view <workdir> --mode raw
python -m docx2typed build <workdir> -o <output.docx>
python -m docx2typed verify <workdir> <output.docx>
python -m docx2typed validate <workdir>
python -m docx2typed normalize <workdir> --candidates
```

Normalization with an explicit policy:

```bash
python -m docx2typed normalize <workdir> \
  --policy <policy.json> \
  -o <normalized.docx> \
  --workdir-out <normalized-workdir>
```

## Workdir contract

`extract` creates one paired, self-contained project:

| File | Purpose | Editable |
|---|---|---|
| `typed.md` | restricted typed source | yes |
| `format.json` | schema, fingerprints, paragraph skeletons, token records | no |
| `styles.json` | content-addressed character style registry | no |
| `_template.docx` | immutable source package | no |

Build, view, verify, and normalize consume the workdir. Do not combine sidecars from different documents.

## Typed editing rules

- Ordinary text is literal after XML entity escaping (`&amp;`, `&lt;`, `&gt;`); Unicode code points and whitespace are preserved.
- Each paragraph body is one logical source line. Word tabs and breaks are explicit `docx-inline` tokens.
- Existing paragraph IDs, base styles, style IDs, token IDs, range attributes, anchor IDs, and opaque references are locked.
- Text inside an existing `<span data-s="...">` retains that style. Empty spans are removed and adjacent equivalent text nodes merge during parsing.
- New paragraphs require `inherit="P0"` (or another existing paragraph ID); deleted paragraphs require `<!--@delete id="P0"-->`.
- Existing hyperlink, comment, and bookmark structure remains paired and fixed while ordinary text may change. Targets and relationships cannot be authored or retargeted.
- Unsupported fields, drawings, revisions, and other opaque nodes are shown as diagnostics. A touched paragraph containing one fails before output publication.
- Styles and structural tokens are not editable in normal text mode.

Do not use CommonMark, generic HTML, arbitrary XML, zero-width characters, or hidden Unicode metadata as editing syntax.

## Verification contract

The required seam is:

```text
source DOCX → extract workdir → edit typed.md → build output DOCX → independent verify
```

`build` fails closed on malformed grammar, structure/style changes, invalid inheritance, missing deletion tombstones, source/template drift, and protected package changes. It writes a temporary DOCX, runs package checks and independent verification, then atomically publishes the output. `verify` independently re-derives the template baseline and checks text, styles, structural tokens, protected XML regions, and every non-document package part.

Normal extraction preserves Unicode superscript/subscript characters separately from Word `vertAlign`. Optional normalization requires a pinned catalog hash, source template fingerprint, occurrence-level decisions, and creates a new DOCX/workdir plus `normalization.audit.json`; it never mutates the original workdir.
