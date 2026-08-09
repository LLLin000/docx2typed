---
name: docx2typed
description: >
  Word DOCX text editing with locked formatting and structure (byte
  fidelity), plus a browser review console and human-to-agent handoff. Use
  when text must change while Word formatting, tracked revisions, comments,
  table structure, hyperlinks, content controls, or package parts must stay
  safe; accept or reject revisions; delete or clear comments; insert/delete
  table rows or columns; merge or split cells; edit content-control text;
  audit Unicode superscript/subscript normalization; or run any extract ->
  edit -> build -> verify DOCX workflow. MCP server available for agent tool
  calls.
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
| Open the browser review console or process human decisions | `composites.md` → Playbook D | review console → human decision → agent queue → build/verify |
| Install or configure the package for an agent | `Installation.md` + the host's skill manager | authorize → install → verify → hand off |

Not sure which workflow? The atoms live in `capabilities.md`; read the
workdir state (`view --mode clean` or `edit status`) first, then pick.

## Human-facing review path

The agent owns installation, document execution, and delivery. The human uses
the browser console to inspect the continuous document, jump from the fixed
review rail to a revision or comment, accept/reject/defer a revision, add a
note, or select text for an agent patch.

The browser is a review and handoff surface, not a DOCX writer:

- `Export decisions` downloads a review decision file from a standalone page.
- `Send to agent` dispatches saved browser decisions and text-anchored patches
  into the server queue; it does not write the DOCX.
- The agent reads the queue, applies changes transactionally, refreshes the
  review snapshot, then builds and independently verifies a new DOCX.
- Comments remain by default. Deleting one requires the user's explicit
  instruction.

When a user asks for this flow, follow Playbook D in `composites.md` instead
of asking the user to edit `typed.md`, manage revision IDs, or install files
into a skill directory.

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

## Agent setup and runtime

When this skill is invoked, use the host's normal skill manager and runtime.
If the user authorizes installation, follow `Installation.md` for the package,
MCP, and optional Tailscale setup. The user does not need to copy `SKILL.md`
or know the host's skill directory.

For a package installation, use the installed entry points:

```bash
docx2typed <command>
docx2typed mcp
docx2typed review WORKDIR --host 127.0.0.1 --port 8876
```

For a one-shot isolated command, use `uvx docx2typed <command>`. A source
checkout may use `python -m scripts <command>` only when the package is not
the intended runtime.

## Real-user session protocol

When an agent operates on behalf of a human, the agent owns setup and
execution while the human owns scope, review decisions, and final acceptance.
Keep implementation details behind the browser and the handoff summary.

1. **Intake** — identify the source DOCX, desired outcome, tracked/direct edit
   preference, comment-retention policy, and whether browser review is wanted.
2. **Set up** — when authorized, install or enable this skill and the package
   through the host's normal mechanisms; configure MCP only with permission.
3. **Protect the source** — copy the DOCX into a new workdir on a scratch
   volume; never edit or overwrite the user's original file.
4. **Baseline report** — extract once, open the workdir once, and report the
   document title, coverage, existing revisions/comments, and unsupported or
   ambiguous structures before changing text.
5. **Round loop** — state the current round's goal; make only region-scoped
   edits; preview and commit; report exactly what changed and what remains.
6. **Human review** — open the browser console. The human selects revisions or
   comments, accepts/rejects/defers, or adds a source-anchored patch or note.
   `Send to agent` queues work; it is not a DOCX write.
7. **Continue** — read the review inbox and preflight, apply queued decisions
   or patches transactionally, preserve original comments, refresh the review
   surface, and report the new snapshot plus remaining queue.
8. **Delivery gate** — after the final round, build a new output DOCX, run
   independent verification, convert it through LibreOffice/Word-compatible
   tooling, and return the output path with a compact evidence summary.

Never call the document "finished" because the browser shows a final view or
because an event was sent. Finished means the delivery gate is green. If a
round is interrupted, resume from the persisted workdir/session snapshot and
describe the pending queue before writing.

Read `docs/rpr-reference.md` to translate rPr XML when planning style
regions.
