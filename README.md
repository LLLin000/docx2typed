# docx2typed — byte-fidelity Word DOCX text editing

Edit text in Word documents while formatting, tracked revisions, comments,
table structure, hyperlinks, content controls, and package parts stay
locked. Untouched content replays byte-identical; only the text you change
moves.

## Install

```bash
python -m pip install -e .
```

## Quick start

```bash
docx2typed extract input.docx -o workdir
docx2typed view workdir --mode clean
docx2typed edit sync workdir        # after editing edit.md (or use MCP)
docx2typed build workdir -o output.docx
docx2typed verify workdir output.docx
```

## Documentation (skill graph)

The skill is organized as a hub plus three reference layers:

- `SKILL.md` — hub: contract, branch table, typed.md edit rules, workdir
  contract, gate summary.
- `capabilities.md` — atoms: every CLI command and MCP tool with exact
  syntax and exit contracts.
- `composites.md` — molecules + playbooks: 7 workflows with steps and
  completion criteria, plus 3 end-to-end scenarios (finalize, tracked
  revision, MCP session).
- `verification.md` — gates: the seam, freshness states, build fail-closed
  list, byte-fidelity checks, LibreOffice interop, dev gates.

Agent-facing entry is `SKILL.md`; this README is the human entry.

## Highlights

- **Tracked revisions**: accept/reject/reinsert singly, or byte-level
  `accept-all`/`reject-all` settlement to a new clean baseline.
- **Comments**: delete one comment (entry + anchors + references) or clear
  all via settlement.
- **Table structure**: insert/delete rows and columns, merge/split cells —
  structure bytes synthesized, cell text never rewritten.
- **Content controls**: `w:sdt` text is editable like body text; control
  structure replays byte-exact.
- **Unicode normalization**: governed `audit scan → policy → approval →
  apply` for superscript/subscript with hash-bound provenance.
- **MCP server** (`python -m docx2typed.mcp_server`): the same engine as
  region-scoped agent tools.

## Development

```bash
python -m pytest -q --basetemp=D:/L/AppData/pytest-tmp
python -m scripts.acceptance_corpus --workdir D:/L/AppData/docx2typed-corpus-run
python -m scripts.tool_smoke --workdir D:/L/AppData/smoke-run
```

See `verification.md` for the full gate contract.
