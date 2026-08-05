# docx2typed — typed-mode DOCX text editing

Typed mode is the experiment branch's only format. It keeps ordinary prose readable while locking Word formatting and document structure.

## Run

Install the checkout once:

```bash
python -m pip install -e .
```

Then run the CLI from any directory:

```bash
docx2typed extract input.docx -o workdir
docx2typed view workdir --mode clean
docx2typed view workdir --mode style
docx2typed view workdir --mode raw
docx2typed build workdir -o output.docx
docx2typed verify workdir output.docx
docx2typed validate workdir
docx2typed audit scan workdir -o scan.json
docx2typed audit apply workdir --scan scan.json --policy policy.json -o normalized.docx --workdir-out normalized-workdir
```

`audit scan` is read-only. `audit apply` is the only governed normalization mutation.

Without installation, run `python -m scripts <command> ...` from this checkout.
The hash-bound `edit.md` projection and `edit sync` workflow are specified in
`docs/prd/typed-mode-word-editing.md` and ADR 0036, but are not implemented by
the current CLI. Until then, `typed.md` is the only writable surface and raw
edits must remain narrow, validated, and structure-aware.

Extraction creates one paired workdir:

```text
workdir/
  typed.md           # current raw typed input; clean projection is not yet implemented
  format.json        # schema, fingerprints, paragraph skeletons, token records
  styles.json        # immutable content-addressed character styles
  _template.docx    # immutable source package
```

Build accepts the workdir, not independently selected sidecars. It rejects changed source/template fingerprints, malformed typed syntax, style or structural mutations, missing deletion tombstones, invalid inheritance, and protected package changes. Output is written through a temporary DOCX and independently verified before publication.

## Typed source

`typed.md` is a restricted project grammar, not CommonMark or generic HTML:

```text
<!--@typed schema="1" format="format.json" styles="styles.json" template="_template.docx" source="input.docx"-->

<!--@p id="P0" base="s_body"-->
普通正文<span data-s="s_bold">加粗文字</span><docx-inline id="N0" kind="tab" style="s_body"/>
```

Rules:

- Ordinary text is literal after XML entity escaping (`&amp;`, `&lt;`, `&gt;`). One paragraph uses one logical source line.
- Existing paragraph IDs, style IDs, token IDs, range attributes, anchors, and opaque references are locked.
- Text inside an existing span may change without losing its style. Empty spans disappear and adjacent equivalent text nodes merge during parsing.
- New paragraphs require `inherit="P0"`; deleted paragraphs require `<!--@delete id="P0"-->`.
- Existing hyperlink/comment/bookmark structure remains fixed while ordinary text changes. Unsupported fields, drawings, revisions, and opaque nodes are diagnostic-only; touching their paragraph fails.
- `clean`, `style`, and `raw` are read-only AST projections.

## Audited Unicode normalization

Normal extraction preserves Unicode superscript/subscript code points and Word `vertAlign` styles as distinct representations. Governed normalization is an explicit scan → policy → apply workflow:

```bash
docx2typed audit scan workdir -o scan.json
docx2typed audit apply workdir \
  --scan scan.json \
  --policy policy.json \
  -o normalized.docx \
  --workdir-out normalized-workdir
```

`scan` only validates the workdir and writes a hash-bound candidate artifact plus `scan.json.run.json`. A policy must contain an explicit `convert` or `preserve` decision, actor, matching candidate fingerprint, and rationale for risky classifications. `audit apply` requires a complete policy with `status="approved"` and an explicit human or self approval record; stale workdir, model, catalog, scanner, or scan bindings are rejected before transformation. Successful output is written to a new DOCX/workdir and includes `normalization.audit.json` plus run evidence. The original source and workdir are never modified.

## Legacy policy-1 Unicode normalization

The legacy `normalize` command is an unaudited compatibility path. It requires the explicit `--legacy-policy-1` flag and emits `governance_status="legacy-unaudited"`; use `audit scan/apply` for governed normalization:

```bash
python -m docx2typed normalize workdir \
  --legacy-policy-1 \
  --policy policy.json \
  -o normalized.docx \
  --workdir-out normalized-workdir
```

Inspect legacy candidates before writing a policy:

```bash
python -m docx2typed normalize workdir --candidates
```

Legacy policies carry the workdir template fingerprint, pinned catalog hash, profile (`selective` or `all`), and every candidate decision. The result includes `normalization.audit.json`; the original source and workdir are never modified.

## Verification

The acceptance seam is:

```text
source DOCX → extract workdir → raw edit typed.md → build output DOCX → independent verify
```

`verify` re-derives the baseline from the fingerprinted template, parses the typed source and output independently, compares text/style/structure, and checks every protected DOCX package part and XML region.
