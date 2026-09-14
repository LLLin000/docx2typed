# PRD: Foreign edit (capability fallback, not a second editor)

Status: draft · 2026-09-14 · branch `feature/foreign-edit-lane`

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
| `_adopt_baseline(baseline, target)` | `scripts/mcp_server.py` | adopt a freshly re-extracted baseline as this workspace's next state |
| `publish_current(origin="baseline-transition")` | same | the version records how the generation came to be, with `baseline_epoch + 1` |
| `_changed_paragraph_texts(left, right)` | same | per-paragraph attribution |
| family/workspace resolution | `scripts/workspace_registry.py` | the returning DOCX proves which document it belongs to (ADR 0046) |
| typed tokens + opaque regions | `format.json` | format-only changes (same text, different tokens) and changes to locked structure (drawings, fields, SDTs) are visible |
| `validate` / `verify` | CLI | the engine's own verdicts, not the external tool's |

`foreign_edit_adopt(candidate, target?)` is the only new public surface. It
runs: resolve the candidate's family → infer the base version → engine
`validate` → re-extract to scratch → attribute the delta → gate → adopt.

There is deliberately **no** `foreign_edit_begin` / `…_finish` session: the
candidate is just `build_docx(version=V)` (already version-addressed), and a
failure anywhere leaves nothing to clean up.

### Base-version inference, not a parameter

The registry already records every managed export with its version
(`managed-export` observations). The candidate is therefore matched against
known exports and the base is the version whose export it differs from
**minimally and coherently**. Ambiguity is the only case that asks.

### Target-first scope

The agent derives the target from the request — "把这张表加一行" is
`target={table: "T2"}`, not a vocabulary of change types:

```
target: ["table:T2"] | ["paragraph:P12..P20"] | ["style:Heading1"] | ["section:3"]
omitted → whole-document mode (an explicit "我手工改过了，收下这个文件")
```

Change-type allow-lists (`add-paragraph`, `style-change`, …) are **not** the
scope language: "其他别动" is verifiable, "only these change types" is not.
Everything outside the target that changes *semantically* is an unexpected
change, whatever its type.

### Four attribution layers + one bucket

| layer | source | example |
|---|---|---|
| paragraph | visible text per paragraph id | one row added, two paragraphs reworded |
| format | typed tokens for the same text | "title now 18pt bold" |
| opaque | locked-region bytes | a drawing or field changed inside an otherwise untouched paragraph |
| package | part name → sha256 over the whole zip | `styles.xml` touched; `theme/theme1.xml` or `customXml/*` dropped |
| normalization | parts re-serialized with no semantic delta | Word's resave footprint |

The package layer is what a semantic diff alone cannot give: a change that is
invisible in text but harmful in the package (dropped `rsid`s, reordered
`sectPr`, lost `mc:AlternateContent`, a vanished `customXml/item1.xml`) has to
be caught by comparing part hashes against an allow-list tied to the target.

### The decision ladder

| situation | outcome |
|---|---|
| delta ⊆ target, no normalization, no opaque/package surprise | **auto-adopt**, report the result |
| delta ⊆ target + explainable extras (e.g. a style definition the target implies) | **one confirmation**, listing the extras |
| delta outside target, unattributable change, opaque/package casualty | **refuse** — not overridable by approval |
| unsaved draft, or the same paragraph changed on both sides | **refuse** (`foreign-edit-conflict`), keep both sides |

Consent is for *explainable extras*, never for *unknown* changes and never for
conflicts. And it is not asked in the normal case: a confirmation that appears
every time gets clicked without reading, which is how a safety mechanism dies.

## Fail-closed rules

1. The external tool only ever edits an export of a version; the live workdir is never handed out. Because an export requires a saved state (ADR 0044), the operation begins with a save boundary — the engine states it as part of the operation rather than asking, and never exports an unsaved draft as if it were a version.
2. Adoption requires the engine's own `validate` + re-extraction; the external tool's verdict is evidence about itself, not about us.
3. Every attributed change must be inside the target; outside means refuse with the concrete list.
4. Untouched regions must replay byte-for-byte against the new baseline, or the generation is refused (`normalization` is a bucket, not an excuse).
5. Conflicts are reported, never auto-merged.
6. The version records the lane, the tool, the full command line, the attribution summary, and the `verify` verdict; old generations and their exports stay recoverable.
7. Whole-document mode exists for "I edited it by hand in Word" — permissive on scope, still attributed, still previewed, still recorded.

## Lane selection

- Native covers paragraph text (`document_patch`, `document_replace`) and span formatting (`format_span`: superscript/subscript/bold/italic) — "把 P12 加粗" is native today.
- Foreign covers block structure (paragraphs, tables, rows/columns), style definitions, sections, headers/footers, numbering, fields/TOC, drawings and part-level changes.
- Every native refusal names the fallback (`next: foreign-edit`) so the agent routes on a fact, not on its own guess about our capabilities.
- `SKILL.md` states the boundary once; the router sends structural/style intent straight to the foreign path.

## Boundary with the native lane

The foreign lane answers "the typed grammar cannot express this" — not "adding
a paragraph is work". Native owns text *and paragraphs*, including the content
of an insert, in one generation (ADR 0047: an edit inside a pending insertion is
absorbed in place, by its own author). Without that, the commonest structural
act — add a paragraph — would leave the engine for a tool call plus a baseline
transition, and the history would fill with transitions caused by ordinary
editing.

| intent | lane |
|---|---|
| text, span formatting, add/delete/edit an inserted paragraph | native |
| table rows/columns, merges (already implemented structurally) | native |
| style definitions, sections, headers/footers, numbering, fields/TOC, drawings, part-level edits | foreign |

Two consequences for this PRD:

- the foreign lane must not be proposed as a fallback for native work; a native
  refusal names the *reason* it cannot express something, and only a genuine
  grammar boundary routes here;
- Microsoft Word interoperability evidence for the absorbed-insertion shapes is
  still outstanding (#88) — the boundary above is a design claim until those
  cells exist.


## Visual verification

For layout and style work, a text diff is not a result the user can accept.
The foreign lane's own renderer (`officecli view html | screenshot`) is the
visual surface: before/after images, plus the attribution summary. We do not
write a second renderer, and we do not treat a rendering as proof of
structure — `verify` and the package diff remain the authority.

## Not in scope

- Wrapping the external tool's command surface (the agent uses it directly; we own the boundary, not the toolbox).
- A general three-way merge engine (conflicts are reported, not resolved).
- Expanding the typed grammar into a Word feature matrix. The narrow mapping (format properties that the span model can express, merged without a transition) is a follow-up, and only after the boundary is in use.

## Measurements that set the defaults

1. Does an external edit preserve untouched `document.xml` byte-for-byte, or re-serialize the part? → default of the `normalization` policy.
2. Is `dump`-vs-`dump` a stable semantic delta? → whether attribution borrows it or relies on our own re-extraction.
3. Does our `extract` + `verify` accept the external output cleanly? → whether a foreign generation can carry the same per-generation evidence.
4. Do externally inserted tracked revisions survive our revision lane? → whether structural work can also be revision-typed.

## Open decisions

Two policy questions gate implementation of the lane itself (they change the
product's guarantee, so they are the user's call):

- **May a baseline transition give up byte-replay identity with the previous
  generation?** It must, or "add a table / change a style" is impossible; the
  cost is that the replay claim is re-rooted at the new baseline and the old
  generation stays as the recoverable record.
- **Admission:** auto-adopt inside the declared target with consent only for
  explainable extras and unattributable changes, or require consent for every
  adoption? (The first is the recommendation in "The decision ladder"; the
  second is the conservative reading.)


- Visual preview in v1, or after the boundary lands? (It needs the external tool's renderer present.)
- `foreign_edit_accept` as its own tool (mirroring the adoption token) or a mode of `foreign_edit_adopt`?
- Is `target` exposed to the agent explicitly (it is derivable), or kept as an optional refinement the agent may omit?
