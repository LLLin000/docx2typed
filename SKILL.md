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
python -m docx2typed edit status <workdir>
python -m docx2typed edit refresh <workdir> [--init] [--discard]
python -m docx2typed edit sync <workdir>
python -m docx2typed normalize <workdir> --candidates
python -m docx2typed audit scan <workdir> -o <scan.json>
python -m docx2typed audit apply <workdir> --scan <scan.json> --policy <policy.json> -o <normalized.docx> --workdir-out <normalized-workdir>
```
## Clean edit state

Extraction now generates a span-free `edit.md` projection plus an
authoritative `edit.state.json` binding (Slice A). `edit.md` is the Agent
reading surface; `typed.md` remains the only canonical writable source. The
`edit.md` header is a visible mirror only — freshness is computed from the
CLI-managed sidecar, and any header/sidecar disagreement fails closed as
`edit-header-tampered`.

Freshness states are `clean`, `dirty` (edit.md changed), `stale-clean`
(typed.md changed without refresh), and `conflict` (both changed). `validate`,
`build`, and `verify` reject every non-clean state; there is no bypass flag.

Raw typed editing flow:

```text
edit typed.md -> docx2typed edit refresh <workdir> -> build -> verify
```

`edit refresh` regenerates the projection after a raw typed change.
`--init` creates the projection for a legacy workdir that already passes
validation; `--discard` replaces a dirty/conflicting draft and records the
discarded hash in evidence. `edit sync` applies an edited `edit.md` draft to
the canonical typed AST: unchanged text keeps its style, inserted text
inherits the caret context (left by default, first visible unit on the right
at paragraph start, `insertion_style` for empty paragraphs), single-style
replacements keep their style, local mixed replacements are accepted only
with an unchanged anchor and use the selection-start style with a warning,
and ambiguous or unanchored mixed rewrites fail closed. Every accepted hunk
is recorded in `edit.state.json.run.json`; `@new`/`@delete` markers insert
and delete paragraphs. After sync the workdir returns to `clean` and builds
normally.


## Typed source examples

`typed.md` is a restricted typed source, not ordinary Markdown. A minimal
document looks like this:

```text
<!--@typed schema="1" format="format.json" styles="styles.json" template="_template.docx" source="source.docx"-->

<!--@p id="P0" base="S1"-->
本发明涉及<span data-s="S2">生物医用材料</span>技术领域。

<!--@p id="P1" inherit="P0"-->
新增段落。

<!--@delete id="P2"-->
```

Use these rules when editing:

- Keep the `@typed` header and every existing `@p` marker unchanged unless
  the requested operation is a paragraph insertion or deletion.
- Text inside `<span data-s="S2">...</span>` owns style `S2`; replace its
  words without removing or moving the wrapper.
- A new paragraph must inherit an existing paragraph, for example
  `<!--@p id="P1" inherit="P0"-->`. Do not invent a `base` style for it.
- Delete an existing paragraph with `<!--@delete id="P2"-->`; do not remove
  its marker and body silently.
- Keep each paragraph body on one logical source line. XML-sensitive text
  uses `&amp;`, `&lt;`, and `&gt;`; do not add Markdown headings or arbitrary
  HTML.

For example, this is a safe style-preserving edit:

```text
原文：普通文字<span data-s="S2">原格式文字</span>。
修改：普通文字<span data-s="S2">替换后的文字</span>。
```

Structural tokens such as `<docx-inline .../>`, `<docx-anchor .../>`, and
`<docx-opaque .../>` are read-only. If a requested change touches one, stop
before `build` and report the affected paragraph.

## Audited Unicode normalization

Normal extraction preserves Unicode superscript/subscript code points and Word `vertAlign` styles as distinct representations. Use the governed scan → policy → apply workflow for normalization:

```bash
python -m docx2typed audit scan <workdir> -o <scan.json>
python -m docx2typed audit apply <workdir> \
  --scan <scan.json> \
  --policy <policy.json> \
  -o <normalized.docx> \
  --workdir-out <normalized-workdir>
```

`audit scan` is read-only and writes a hash-bound scan artifact plus run evidence. Policies require explicit occurrence-level `convert` or `preserve` decisions, actors, matching fingerprints, and rationale for risky classifications. `audit apply` requires a complete policy with `status="approved"` and an explicit `human` or `self` approval object. Stale workdir, model, catalog, scanner, or scan bindings fail before transformation. Successful apply creates a new DOCX/workdir and writes `normalization.audit.json`; the original workdir is unchanged.

### Agent protocol for audit normalization

1. Run `audit scan` first. Read the candidates and show the proposed
   conversions before changing anything.
2. Treat classification as a suggestion, never as a decision. For every
   occurrence choose exactly `convert` or `preserve`. If the classification is
   ambiguous, manual, unsupported, non-reversible, or style-conflicting,
   require a rationale; when uncertain, preserve and ask the user.
3. Build the policy from the scan artifact. Copy snapshot fields,
   `scan_artifact_sha256`, each `occurrence_id`, and each
   `candidate_fingerprint` exactly; never invent hashes or IDs.
4. Keep the policy `draft` or `reviewed` while decisions or review are
   pending. Do not run `audit apply` until every candidate is decided and the
   policy has `status="approved"` plus a valid approval object.
5. Use `approval_requirement="human"` by default. Use `self` only when the
   user explicitly authorizes agent self-approval; otherwise stop and request
   approval. `audit apply` writes a new DOCX/workdir and never mutates the
   source workdir.

### Policy skeleton

Start from the scan artifact and replace every placeholder. This is a
structure guide, not a runnable policy:

```json
{
  "schema": "vertical-normalization-policy-2",
  "status": "draft",
  "approval_requirement": "human",
  "audit_schema": "vertical-normalization-audit-2",
  "scanner_contract_version": 1,
  "project_id": "<scan.snapshot.project_id>",
  "baseline_sha256": "<scan.snapshot.baseline_sha256>",
  "draft_snapshot_sha256": "<scan.snapshot.draft_snapshot_sha256>",
  "model_sha256": "<scan.snapshot.model_sha256>",
  "catalog_sha256": "<scan.snapshot.catalog_sha256>",
  "scan_artifact_sha256": "<scan.scan_artifact_sha256>",
  "decisions": {
    "<candidate.occurrence_id>": {
      "decision": "preserve",
      "actor": "<reviewer>",
      "candidate_fingerprint": "<candidate.candidate_fingerprint>",
      "rationale": "<required for risky classifications>"
    }
  }
}
```

There must be one decision object for every scan candidate. A `convert`
decision is valid only for an `approved` classification with a non-empty
`proposed_target`. Before apply, add the explicit approval object and change
the status:

```json
{
  "status": "approved",
  "approval": {
    "approved": true,
    "requirement": "human",
    "approved_by": "<approver>",
    "approval_time": "<UTC ISO-8601 timestamp>"
  }
}
```

The legacy `normalize` command is an unaudited compatibility path. It requires `--legacy-policy-1` and emits `governance_status="legacy-unaudited"`. Agents must use `audit scan/apply` when approval and provenance are required.

Legacy policy-1 command:

```bash
python -m docx2typed normalize <workdir> \
  --legacy-policy-1 \
  --policy <policy.json> \
  -o <normalized.docx> \
  --workdir-out <normalized-workdir>
```

## Workdir contract

`extract` creates one paired, self-contained project:

| File | Purpose | Editable |
|---|---|---|
| `typed.md` | restricted typed source (canonical) | yes |
| `edit.md` | span-free Agent projection (Slice A) | no mutation yet |
| `edit.state.json` | authoritative freshness binding | no |
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

Normal extraction preserves Unicode superscript/subscript characters separately from Word `vertAlign`. Governed audit normalization binds each occurrence decision to the source snapshot, scanner, catalog, and explicit approval; it creates a new DOCX/workdir plus `normalization.audit.json` and never mutates the original workdir.
