---
name: docx2typed
description: >
  Word DOCX text editing with locked formatting and structure (byte
  fidelity). Use when text must change while Word formatting, tracked
  revisions, comments, table structure, hyperlinks, content controls, or
  package parts must stay safe; accept or reject revisions; delete or
  clear comments; insert/delete table rows or columns; merge or split
  cells; edit content-control text; audit Unicode superscript/subscript
  normalization; or run any extract -> edit -> build -> verify DOCX
  workflow. MCP server available for agent tool calls.
---

# docx2typed — byte-fidelity Word text editing

The contract is **byte fidelity**: untouched content replays byte-identical;
only the text you change moves. Editing is typed-mode (continuous prose +
locked structural tokens), never raw XML surgery.

## Skill graph

This skill is structured as a graph of four files — a hub plus three
reference layers, each with explicit dependency edges:

```text
SKILL.md ──────────────── hub: invocation, rules, branch table, gates
├── capabilities.md ──── ATOMS (工具): every CLI command + MCP tool,
│                         exact syntax and exit contract. No dependencies.
├── composites.md ────── MOLECULES (工作流): 7 workflows that chain atoms,
│                         each with ordered steps and completion criteria;
│                         plus 3 end-to-end playbooks.
│                         depends on: capabilities.md atoms, verification.md gates
└── verification.md ──── GATES (检查): the shared acceptance contract every
                          workflow ends on. Applied by all composites.
```

The graph works by composition, not nesting: a workflow names the atoms it
uses and the gates it ends on; you never open more than one layer deep from
the hub.

## Branch table — where to start

| Task | Open | Flow |
|---|---|---|
| Change text (plain, tracked, or inside content controls) | `composites.md` → Workflow 1/2/7 | edit → sync → build → verify |
| Accept / reject tracked revisions | → Workflow 3 | decide accept/reject → build → verify |
| Delete one comment or clear all comments | → Workflow 4 | decide comment-delete / accept-all → verify |
| Insert/delete table rows or columns, merge/split cells | → Workflow 5 | decide table-* → new baseline → verify |
| Unicode superscript/subscript normalization | → Workflow 6 | audit scan → policy → approval → apply |
| End-to-end finalize / revise / agent session | `composites.md` → Playbooks | workflow chains + full gate set |

Not sure which workflow? The atoms live in `capabilities.md`; read the
workdir state (`view --mode clean` or `edit status`) first, then pick.

## The edit rules (apply to every editing workflow)

`typed.md` is a restricted typed source, not Markdown. Minimal document:

```text
<!--@typed schema="1" format="format.json" styles="styles.json" template="_template.docx" source="source.docx"-->

<!--@p id="P0" base="S1"-->
本发明涉及<span data-s="S2">生物医用材料</span>技术领域。

<!--@p id="P1" inherit="P0"-->
新增段落。

<!--@delete id="P2"-->
```

Rules:

- **Only text moves.** Keep the `@typed` header and every `@p` marker
  unchanged unless the operation is a paragraph insertion or deletion.
- Text inside `<span data-s="S2">…</span>` owns style `S2`; replace its
  words without touching the wrapper. Empty spans and adjacent same-style
  text merge automatically during parsing.
- New paragraph: `<!--@p id="P1" inherit="P0"-->` — inherit an existing
  paragraph, never invent a `base` style.
- Delete: `<!--@delete id="P2"-->` — never remove a marker and body
  silently (missing tombstone is a validation error).
- One paragraph = one logical source line. XML-sensitive text uses
  `&amp;` `&lt;` `&gt;`. No CommonMark, no generic HTML, no zero-width
  characters.
- Structural tokens (`<docx-inline …/>`, `<docx-anchor …/>`,
  `<docx-opaque …/>`) and revision containers are read-only. A change
  touching one: stop before `build` and report the paragraph.
- Content controls (`w:sdt`) expose their paragraphs as `S0.P0`-style ids
  and are editable like body text; the `sdtPr` structure replays byte-exact.
- Table cell paragraphs are `T0.R0.C0.P0`-style ids and editable like body
  text; table structure itself is changed only via `decide table-*`
  (Workflow 5), never by editing tokens.

## Workdir contract

`extract` creates one self-contained project; build/verify/decide consume it
as a unit — never combine sidecars from different documents.

| File | Purpose | Editable |
|---|---|---|
| `typed.md` | canonical typed source | yes |
| `edit.md` | span-free agent projection / patch input | via `edit sync` or MCP |
| `edit.state.json` | authoritative freshness binding | no |
| `format.json` | fingerprints, paragraph skeletons, token records | no |
| `styles.json` | content-addressed style registry | no |
| `_template.docx` | immutable source package | no |

## Gates (summary — full contract in `verification.md`)

- **clean gate**: `validate`, `build`, `verify` reject every non-clean edit
  state; there is no bypass flag.
- **verify is independent**: `verify` re-derives the baseline from the
  fingerprinted template and compares text, styles, tokens, protected XML
  regions, and every package part — it does not trust `build`.
- **byte fidelity**: a no-op build must be byte-identical to the input;
  untouched paragraphs replay raw bytes.
- **interop**: outputs must open in LibreOffice/Word (convert to PDF with
  `soffice --headless --convert-to pdf` before delivering).

## Run

```bash
cd ~/.omp/agent/skills
python -m docx2typed <command>          # CLI (full reference: capabilities.md)
python -m docx2typed.mcp_server         # stdio MCP server (same engine)
```

Read `docs/rpr-reference.md` to translate rPr XML when planning style
regions.
