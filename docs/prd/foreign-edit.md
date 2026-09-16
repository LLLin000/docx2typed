# PRD: Foreign edit (capability fallback, not a second editor)

Status: F0 landed on `feature/foreign-edit-lane` · receipt + adopt + attribution + consent gates + native refusal routing · 2026-09-16 · OfficeCLI/soffice qualification and visual evidence pending real-tool evidence.

## Problem Statement

The engine is safe because it refuses what it cannot prove. The cost lands on
the user as a cliff:

> "The moment I touch a table, a style, an image, or the layout, I have to
> leave this system, edit in Word or another tool, and when I come back the
> history is gone, the workspace is gone, and nobody can tell me whether
> something else got damaged on the way."

The pain is not "docx2typed cannot change a format". It is that **there is no
safe way to have the work done elsewhere and come back**. Users end up with
`final.docx`, `final2.docx`, `final_改格式.docx`, `老师修改.docx` — the same
document, four identities, no lineage.

Two rejected answers:

- **Grow the native writer** (bold → font size → sections → images → tables):
  this ends as a half-finished Word engine, and the property that makes the
  product worth using — *untouched regions are provably untouched* — gets
  weaker with every capability we add.
- **Tell the user to leave and re-extract** (today's state): structurally
  possible, but it drops identity, attribution and history at exactly the
  moment the document matters most.

## Product definition

> **The user says what to change. The system chooses the lane. The foreign
> work is fully audited, scope-controlled and reversible, and the document is
> still the same document afterwards.**

The user never chooses between "native lane" and "foreign lane", never writes
a scope, never sees `baseline epoch`. What they see is a capability fallback:
an edit that needs another tool still completes inside the same document
timeline.

```
User: "把这张表下面增加一行，并把标题改成小二号黑体，其他别动。"

  native capability check ──(expressible)──→ native tools
        │
        └─(structural / style-definition / package-level)
             derive the target from the request
             candidate = export of the base version        (build_docx)
             external tool edits the candidate             (officecli …)
             validate → re-extract → attribute → gate      (foreign_edit_adopt)
             no unexpected change  → adopt as the next version
             unexpected change     → one short confirmation, or refuse
             conflict              → refuse, both sides kept

User sees:
  已完成：表格新增 1 行；标题改为小二号黑体。
  未检测到范围外的内容或结构变化。已保存为 V23，可以继续修改正文。
```

Two lanes, two promises, never mixed inside one generation:

| | Native epoch | Foreign transition | New native epoch |
|---|---|---|---|
| who edits | the engine's typed writer | any external tool | the engine again |
| promise | untouched paragraphs replay byte-for-byte | every change attributed; scope gated; old generations remain recoverable | byte-for-byte replay, re-rooted at the new baseline |
| evidence | `verify` per paragraph | attribution report + package diff + `verify` on the new baseline | same as native |

A baseline transition does not weaken the fidelity claim; it **re-roots** it.
`verify` on a generation has always been relative to that generation's
baseline. What is lost across a transition is identity with the *old* bytes —
which no tool can promise anyway once Word has opened and saved the file.

## What the user must not see

Internal and product-facing vocabulary are different layers:

| Internal concept | What the user gets |
|---|---|
| `foreign_edit_adopt`, candidate path, base version | "我用外部结构编辑器改了一下，正在核对" |
| scope JSON | nothing — derived from the request by the agent |
| `baseline_epoch`, `lane`, normalization policy | one line in the result: "已保存为 V23" |
| attribution report | three to five human-readable lines, only if something is off |

## Mechanics (existing primitives, one new boundary)

Most of the seam already exists — `table_*` and `decide_all` run exactly this
shape today:

| Piece | Where | Role |
|---|---|---|
| version-addressed export | `build_docx(version=…)` | the candidate the external tool edits; the live workdir is never touched |
| candidate receipt | `foreign_edit_prepare` | engine-owned provenance binding the candidate to one family/workspace/version/target |
| `_adopt_baseline(baseline, target)` | `scripts/mcp_server.py` | adopt a freshly extracted baseline as this workspace's next state |
| `publish_current(origin="baseline-transition")` | same | the version records how the generation came to be, with `baseline_epoch + 1` |
| paragraph anchors | receipt + extracted `format.json` | deterministic identity attribution; never a similarity-based base guess |
| typed tokens + opaque regions | `format.json` | format-only changes and changes to locked structure are visible |
| package inventory | candidate and base package manifests | added/removed parts, relationships, content types, and opaque casualties are classified |
| `validate` / `verify` | CLI | the engine's own verdicts, not the external tool's |

The public foreign interface has exactly two tools:

```text
foreign_edit_prepare(target, version?)
foreign_edit_adopt(candidate, candidate_id?, consent_token?)
```

There is deliberately no `foreign_edit_begin` / `…_finish` session and no
generic external-process runner. The candidate is a version export; the agent
may call an external tool directly. The engine owns provenance, candidate
freshness, attribution, gates, and adoption.

### Candidate receipt

`foreign_edit_prepare` requires a clean saved Version, materialises its
version-addressed export, derives the target anchor set, and writes one
engine-owned receipt beside the candidate (or in the engine's candidate
evidence directory). The receipt is provenance for one candidate, not a
separate history or lease:

```json
{
  "schema": "docx2typed-foreign-candidate-1",
  "candidate_id": "FC37",
  "family_id": "f_…",
  "workspace_id": "ws_…",
  "base_version": "V17",
  "base_commit": "…",
  "base_tree": "…",
  "base_export_sha256": "…",
  "target": ["style:Heading1"],
  "target_digest": "…",
  "anchor_set": "…",
  "created_at": "…",
  "candidate_original_sha256": "…"
}
```

The receipt must pin the base; `foreign_edit_adopt` MUST NOT compare managed
exports and choose the one with the smallest or most coherent diff.

### Controlled and manual entry

The normal path is:

```text
foreign_edit_prepare(target, version=V17)
    → candidate + FC37
external tool edits candidate
foreign_edit_adopt(candidate, candidate_id=FC37)
```

`FC37` proves `f_… / ws_… / V17`; no base inference is needed.

`foreign_edit_adopt(candidate)` without a receipt is the manual external
adoption path. It uses the Family Resolver only to produce lineage candidates:

```text
exact known artifact       → deterministic evidence
file identity continuity   → candidate only
metadata hint              → candidate only
no proof                   → ask once
```

Manual adoption MUST require an explicit `family_id` and `base_version`
selection before it can create a synthetic receipt and enter the same
attribution pipeline. Similarity may never select either value.

### Candidate identity

The receipt stores a baseline anchor set for the target and relevant
untouched regions:

```text
typed paragraph id
+ part
+ OOXML location
+ w14:paraId when present
+ structural fingerprint
+ neighbour identities
```

Candidate attribution uses this deterministic order:

```text
stable external ID exact match
→ whole-ID-set paragraph identity (no renumbering happened)
→ unique structural identity
→ byte-identical visible content
→ deterministic document-order pairing
→ ambiguity => refuse
```

The engine MUST NOT use a similarity threshold or minimum-diff heuristic to
choose a base or silently map ambiguous paragraphs. A scratch re-extract may
renumber paragraphs; old and new IDs are not assumed equal. Order pairing is
deterministic, not a guess: every paragraph on both sides still lands in the
change set, so a mispairing surfaces as attributed changes rather than a
hidden delta.

### Adoption operation

`foreign_edit_adopt` runs:

```text
receipt/manual lineage
→ candidate digest and freshness check
→ current HEAD/base conflict check
→ engine package prevalidation
→ re-extract candidate to scratch
→ semantic/format/opaque/package attribution
→ normalization classification
→ target and decision gates
→ safe: _adopt_baseline + publish_current(origin="baseline-transition")
```

A failed adoption leaves the old workspace and candidate intact. The old
Version remains recoverable; the successful transition keeps the same family,
workspace, and timeline while re-rooting the baseline.

### Base-version binding

For a prepared candidate, `base_version` comes only from the receipt and the
receipt is checked against the current workspace identity and
`base_export_sha256`. No diff-based inference is allowed.

For a manual candidate, the resolver may nominate candidates, but the user
must choose both family and base Version. The choice is then captured in a
synthetic receipt before attribution. If the candidate or receipt changes
while a consent/adoption question is open, adoption fails closed.

### Target-first scope

The agent derives the target from the request — "把这张表加一行" is
`target={table: "T2"}`, not a vocabulary of change types:

```text
target: ["table:T2"] | ["paragraph:P12..P20"] | ["style:Heading1"] | ["section:3"]
omitted → whole-document mode (an explicit "我手工改过了，收下这个文件")
```

The lane is selected by one criterion: **can the current canonical model
express the requested operation while maintaining the current epoch
invariants?** Change-type allow-lists are not the scope language. "其他别动"
is verifiable; "only these change types" is not. Everything outside the target
that changes semantically is unexpected, whatever its type.

### Four attribution layers + classified package evidence

| layer | source | example |
|---|---|---|
| semantic | visible text and canonical structure | one row added, a paragraph moved, a style definition changed |
| format | typed tokens and effective style semantics | "title now 18pt bold" |
| opaque | locked-region bytes | a drawing, field, SDT, or unknown container changed |
| package | part inventory, relationships, content types, and hashes | `styles.xml` touched; `customXml/item1.xml` dropped |
| normalization | proven semantic-equivalent serialization churn | XML reordering or canonical lexical rewrite with no semantic delta |

Package changes are classified, not dismissed as generic "Word rewrote it":

- **Known structured parts** may be labelled normalization only when the
  engine's semantic signature proves equivalence.
- **Opaque parts** with a raw change outside the target are not auto-equivalent;
  they are refused unless a future explicit capability owns them.
- Part added/removed, relationship retargeted, content type changed, or an
  opaque/package casualty is an explicit package delta.

The package layer remains necessary because a text-equivalent save can still
drop `customXml`, media, relationships, `mc:AlternateContent`, or other
protected content.

### Decision ladder

| situation | outcome |
|---|---|
| target semantic delta, plus only proven semantic-equivalent serialization churn | **auto-adopt**, record the normalization footprint |
| target semantic delta plus a necessary, explainable semantic dependency | **one confirmation**, listing the dependency |
| target-outside semantic change | **refuse** |
| opaque/package content lost or changed without an owning capability | **refuse** |
| current HEAD has left the receipt's base Version | **refuse** (`foreign-edit-conflict`) |
| same paragraph changed on both sides or deterministic identity is ambiguous | **refuse** (`foreign-edit-conflict` / `ambiguous-alignment`) |
| candidate, receipt, or consent digest changed | **refuse** (`foreign-consent-stale`) |

Normalization is an evidence category, not a failure category. Consent is only
for explainable semantic dependencies; it never overrides an unknown change,
an opaque/package casualty, a conflict, or ambiguous identity.

### External evidence privacy

The version record stores tool identity and a redacted invocation summary, not
the raw command line:

```json
{
  "tool": "officecli",
  "version": "1.0.xxx",
  "schema_fingerprint": "…",
  "binary_sha256": "…",
  "argv_redacted": ["batch", "<candidate>", "--input", "<managed-file>"],
  "argv_sha256": "…",
  "exit_code": 0
}
```

User paths, URLs, tokens, passwords, and other secret-bearing arguments must
not enter permanent provenance. The engine trusts candidate bytes and its own
verification, never the external tool's success message.

## Controlled execution is outside the engine

The engine does not expose `run_external_tool(argv)`. In v1 the agent/skill
invokes OfficeCLI, soffice, Word, WPS, or a user script outside the engine and
then calls `foreign_edit_adopt`. If a future product integration needs a
runner, it must be a fixed-tool adapter with structured arguments, redacted
evidence, isolation, timeout, and process-tree cleanup — not a generic
subprocess surface.

## Fail-closed rules

1. The external tool only ever edits an export of a receipt-pinned Version; the
   live workdir is never handed out.
2. `foreign_edit_prepare` refuses dirty draft/version state. Adoption refuses
   a HEAD that no longer matches the receipt's base.
3. Adoption requires the engine's own package prevalidation, `validate`,
   re-extraction, attribution, and `verify`; the external tool's verdict is
   evidence about itself, not about us.
4. Every semantic delta must be inside the target. Outside semantic change
   means refuse.
5. Known-part serialization churn auto-passes only when our semantic
   signature proves equivalence; normalization is recorded with its footprint.
6. Opaque/package changes without an owning capability, lost parts, relationship
   retargeting, and ambiguous identity are not overridable by approval.
7. Conflicts are reported, never auto-merged.
8. The version records the lane, tool identity, redacted invocation evidence,
   receipt/base, attribution summary, normalization footprint, and `verify`
   verdict; old generations and candidates stay recoverable.
9. Whole-document mode exists for an explicitly user-approved manual file; it
   remains attributed, digest-bound, and fail-closed on unknown/opaque damage.

## Lane selection

- Native covers any operation the current canonical model can express while
  preserving epoch invariants: paragraph text, paragraph insert/delete/edit,
  existing span formatting, revisions/comments, and the already-supported
  table row/column/merge/split operations.
- Foreign covers style definitions, sections/page setup, drawings/images,
  fields/TOC, unsupported numbering/layout, and package-level operations that
  the canonical model cannot express safely.
- "Structure" is not itself a foreign trigger. A structural operation is
  foreign only when the canonical model cannot represent it and maintain its
  invariants.
- Every native refusal names the fallback (`next: foreign-edit`) so routing is
  based on a capability fact.

## Boundary with the native lane

The foreign lane answers "the canonical grammar cannot express this" — not
"adding a paragraph is work". Native owns text and paragraph operations,
including the content of an insert, in one generation. Table row/column and
merge/split operations already have a governed structural lane and remain
native from the user's perspective, even though they re-root the baseline.

| intent | lane |
|---|---|
| text, span formatting, add/delete/edit an inserted paragraph | native |
| revisions and comments through existing governed operations | native |
| table rows/columns, merges/splits already implemented | native |
| style definitions, sections/page setup, headers/footers not represented by the canonical operation | foreign |
| drawings/images, fields/TOC, unsupported numbering/layout, package-level edits | foreign |

The criterion is canonical-model expressibility plus epoch invariants, not a
text-versus-structure label. Microsoft Word interoperability evidence for the
absorbed-insertion shapes remains outstanding (#88).

## Visual verification

For layout and style work, a text diff is not a result the user can accept.
OfficeCLI HTML/screenshot and LibreOffice PDF are visual evidence only.
`verify`, semantic attribution, and the classified package diff remain the
structural authority.

## Not in scope

- Wrapping the external tool's command surface in v1.
- A generic `run_external_tool` or arbitrary subprocess API.
- A general three-way merge engine; conflicts are reported, not resolved.
- Diff-based base-version inference.
- Expanding the typed grammar into a Word feature matrix.
- Treating raw XML, `dump→batch`, or a renderer as a fidelity proof.

## Measurements that set the defaults

1. Does the receipt anchor set survive OfficeCLI/Word/WPS/LibreOffice edits?
   Which stable IDs and structural identities remain available?
2. Which known structured parts show semantic-equivalent serialization churn
   under each external tool?
3. Does our `extract` + `verify` accept the external output cleanly?
4. Do external inserted tracked revisions survive the revision lane?
5. Does the agent experience justify a future fixed-tool runner, or is direct
   external invocation sufficient?

## Implementation decisions (frozen)

- Base lineage is receipt-pinned for prepared candidates; manual candidates
  require explicit family and base-version selection. Similarity never decides.
- Normalization is recorded evidence and auto-adopts only when our semantic
  signature proves equivalence.
- Public surface is exactly `foreign_edit_prepare` and
  `foreign_edit_adopt`; there is no separate `foreign_edit_accept`.
- v1 does not use a resident process or expose a generic process runner.
- OfficeCLI v1 uses direct mode, `batch` atomic only, minimum version
  `>= 1.0.137` when qualification begins; `--best-effort`, `--force`,
  `raw-set`, and `add-part` are outside auto-adopt.
- LibreOffice v1 is renderer/converter only; DOCX resave and UNO mutation are
  later foreign writers.

