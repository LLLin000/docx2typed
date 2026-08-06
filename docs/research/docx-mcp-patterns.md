# docx-mcp-patterns — architecture notes from the most mature Word/.docx MCP server

Study for building **docx2typed-mcp**: an MCP server whose backend is the existing
Python DOCX editing engine (`docx2typed`: typed workdir + `edit.md` + sync + build).
All line citations below refer to the cloned primary repo at the pinned commit.

---

## Repo and commit

- **Primary repo:** `GongRzhe/Office-Word-MCP-Server` (PyPI name `office-word-mcp-server`)
  - **Commit studied:** `a3bbbb6d6167e68cf855d73ef7dc6cd8cfbfedba` (2025-12-31, tag v1.1.11, `chore: bump version to 1.1.11`)
  - **Why it is the most mature:** 2117★ / 280 forks / 66 open issues at study time (2026-08-06); MIT; on PyPI as `office-word-mcp-server` 1.1.11; listed on Smithery and Glama; the de-facto community reference for Word MCP servers. It is **archived** (archived 2026-08-04, last push 2025-12-31) — frozen but proven, which is the correct meaning of "mature" for pattern extraction. No commit activity in 8 months prior to archival.
  - **Runner-up:** `ykarapazar/word-mcp-live` (179★, active, 114 tools) — structurally a superset/derivative of the primary (identical module layout `word_document_server/{tools,core,utils}`, identical `filename`-first stateless tool signatures in `main.py`), adding a Windows COM "live editing" layer. Studied lightly for the session-state contrast (see State section); it does not change the primary's design conclusions for our purposes.
  - **Search evidence:** `gh search repos "docx mcp"` + `"word mcp"` + `"office word mcp"` (2026-08-06) returned GongRzhe at 2117★ (next-highest docx-specific: SecurityRonin/docx-mcp 40★, LegalRabbit-AI/legalrabbit-docx-mcp 47★, UseJunior/safe-docx 37★, hongkongkiwi/docx-mcp 32★, knorq-ai/docx-mcp-server 4★). By stars, forks, and ecosystem adoption the GongRzhe server wins by two orders of magnitude.
  - Clone location: `/tmp/librarian-office-word` (shallow, `--depth 1`).

## Architecture (module layout, entrypoint, transport)

Layout (root listing + `word_document_server/`):

```
word_mcp_server.py                      # 2-line entrypoint -> run_server()
word_document_server/
  main.py                               # 759 lines: FastMCP app, get_transport_config(),
                                        #   register_tools() with 54 @mcp.tool defs, run_server()
  tools/                                # per-capability tool modules (thin async wrappers)
    document_tools.py  content_tools.py  format_tools.py  protection_tools.py
    footnote_tools.py  extended_document_tools.py  comment_tools.py
  core/                                 # reusable doc logic (style/table/footnote/protection ops)
    styles.py  tables.py  footnotes.py  comments.py  protection.py  unprotect.py
  utils/                                # file + document helpers
    file_utils.py  document_utils.py  extended_document_utils.py
pyproject.toml                          # name office-word-mcp-server v1.1.11; python>=3.11
```

- **Entrypoint:** `word_mcp_server.py:1-7` calls `word_document_server.main.run_server`; `main.py:696-750` `run_server()` registers tools then dispatches on transport.
- **Transport:** stdio by default; `streamable-http` and `sse` selectable via env (`MCP_TRANSPORT`, `MCP_HOST/PORT/PATH/SSE_PATH`), `main.py:30-62` `get_transport_config`; `main.py:720-748` calls `mcp.run(transport=...)` (FastMCP 2.8.1+). FastMCP is the single server framework; no raw SDK session handling.
- **Framework/deps:** `pyproject.toml:20-27` — `python-docx>=1.1.2`, `fastmcp>=2.8.1`, `msoffcrypto-tool`, `docx2pdf`, `pytest`. So the "backend" is literally python-docx; the MCP layer is FastMCP decorators over thin wrappers.
- **Tool → implementation split:** every `@mcp.tool` in `main.py` is a 1-3 line pass-through to an `async` function in `tools/*.py`, which itself either implements logic inline or calls into `core/*.py` / `utils/*.py` helpers. `tools/content_tools.py:463-481` shows the pure pass-through pattern for the "near text" helpers.

## Tool definition and validation patterns

- **Declaration:** FastMCP `@mcp.tool` decorators with `annotations=ToolAnnotations(title=..., destructiveHint=True, readOnlyHint=True)`; input schema comes from the wrapped function's Python type hints + docstring (`main.py:96-109` `create_document`), so schemas are `filename: str` plus typed optionals — no JSON-schema hand-authoring. 54 tools registered (`grep -c "@mcp.tool" main.py` → 54).
- **Every mutating tool takes `filename` as the first positional parameter** (`main.py:101`, `main.py:271` `delete_paragraph`, `main.py:305` `format_text`, ...). There is no session/handle abstraction: the file path IS the resource identifier.
- **Validation is manual, in the wrapper, not schema-driven:**
  - existence check: `if not os.path.exists(filename): return "Document ... does not exist"` (`tools/content_tools.py:17-23` header of `add_heading`; repeated verbatim in every tool)
  - writeability check: `check_file_writeable(filename)` before any mutation (`tools/content_tools.py:24-29`; impl `utils/file_utils.py:9-44` — os.access + open-for-append probe, i.e. a "file locked?" test)
  - extension coercion: `ensure_docx_extension()` appends `.docx` if missing (`utils/file_utils.py:73-81`)
  - numeric casts + range checks: `int()` coercions with friendly error strings (`tools/content_tools.py:17-21` level; `tools/format_tools.py:25-36` paragraph_index/start/end), bounds checks against `len(doc.paragraphs)` (`tools/content_tools.py:395-409` `delete_paragraph`)
  - index-or-text dual addressing: paragraph targets may be given as `target_text` (first match, skipping TOC-styled paragraphs) or `target_paragraph_index` (`utils/document_utils.py:194-241` `insert_header_near_text`); the "insert near text" family resolves the anchor inside the same open doc before mutation.
- **Style input is by style *name* string** (`style: str = None` on `add_paragraph` `tools/content_tools.py:113-174`, applied via `paragraph.style = style` with `KeyError → fall back to 'Normal'` + a status string noting the fallback), or by direct run-formatting kwargs (font_name/font_size/bold/italic/color). Custom styles are created by name with an explicit base style (`tools/format_tools.py:135-190` `create_custom_style` → `core/styles.py:66-130` `create_style`).
- **Return convention: human-readable status strings**, not structured results. Success: `"Heading '...' (level N) added to ..."` (`tools/content_tools.py:105`); failure: `"Failed to add heading: {str(e)}"` with the exception string embedded (`tools/content_tools.py:110`). A few "robust" paths return `(bool, str, dict)` tuples (`core/footnotes.py:283-401` `add_footnote_robust` returns `(True, msg, details_dict)`), but the MCP-facing wrapper stringifies them. `readOnlyHint`/`destructiveHint` annotations are the only machine-readable signals.

## State management and concurrency

- **Stateless, path-addressed, open-modify-save per call.** There is no session, no workdir, no in-memory document cache, no lock. Every call: `doc = Document(filename)` → mutate → `doc.save(filename)` → return string. Canonical evidence: `tools/content_tools.py:33-108` (`add_heading`: open at line 33, save at line 105), `tools/document_tools.py:14-48` (`create_document`: `doc = Document()` then `doc.save(filename)`), `utils/document_utils.py:194-241` (`insert_header_near_text`: open → insert → `doc.save(doc_path)` at line 238). "Session per path" question: **answer is no — there is no session; the path is passed on every call and the file is the only state.**
- **Concurrency:** no locks anywhere; two parallel MCP tool calls on the same path would race at `doc.save(filename)` (last-writer-wins whole-file overwrite; python-docx round-trips the entire package on save). The server relies on the MCP client's serialization discipline. `check_file_writeable`'s open-for-append probe is an advisory pre-flight, not a lock.
- **Runner-up contrast (`word-mcp-live`):** the cross-platform set is the same stateless design (verified in its `main.py` — identical `filename`-first signatures). Only the Windows COM layer holds live state, and that state lives in the *Word application* (COM), not in the server: `core/word_com.py` helpers `get_word_app()` / `find_document(app, filename)` resolve a filename to an already-open Word document (`tools/live_tools.py:25-138` `word_live_insert_text`), and edits are applied via COM ranges with `doc.TrackRevisions`/`app.UserName` toggling for tracked changes and an `undo_record` context manager. Even there, "session" = the external Word process, keyed by filename (`filename: str = None` with `None = active document`).
- **Save semantics:** always write-back-in-place to the same path (`doc.save(filename)`); optional `output_filename`/`save_as` on a few tools (footnote add/delete, `convert_to_pdf`) which copy the source first (`core/footnotes.py:283-301` `working_file = output_filename if output_filename else filename; shutil.copy2(...)`). There is no atomic temp-file+rename at the MCP layer (the footnote "robust" path does use `temp_file = working_file + '.tmp'` + `os.replace`, `core/footnotes.py:453-475` — the only atomic publish in the codebase).

## Error handling (structured errors, partial failure, atomicity)

- **Errors are returned as strings, never raised to the MCP client.** Every wrapper catches `Exception` broadly and returns `f"Failed to ...: {str(e)}"` (`tools/content_tools.py:107-110`, `tools/document_tools.py:44-48`, `utils/document_utils.py:13-39` `get_document_properties` → `{"error": ...}` dict). There is no `mcp.types` error, no error-code taxonomy, no structured `isError` result.
- **Consequence for agents:** failure detection is *string parsing* — the model must grep the message for "Failed"/"does not exist"/"Invalid". Partial failures (e.g. style fallback) are *silently degraded* with only a prose note: `"Style '...' not found, paragraph added with default style"` (`tools/content_tools.py:113-174` `add_paragraph`).
- **Atomicity:** none at the MCP layer. A crash between open and save loses the whole call's changes; a crash inside the multi-file footnote path can leave a `.tmp` (they clean it in `except` in `core/footnotes.py:467-472`). Only `core/footnotes.py:453-475` does atomic replace; the standard path `doc.save(path)` is a single python-docx round-trip (python-docx writes via temp+replace internally — that is the *library's* atomicity, not the server's).
- **Structured-ish exceptions:** `add_footnote_robust` returns `(False, "reason", None)` tuples for validation failures *before* mutation (`core/footnotes.py:283-301`: "Must provide either search_text or paragraph_index", "Cannot provide both", "File not found"), which is the closest thing to structured validation in the repo — but it is per-function and not surfaced structurally through MCP.

---

## Directly reusable patterns for docx2typed-mcp (concrete, with citations)

### What our backend does better (and why the MCP layer must not copy the GongRzhe model)

| Concern | GongRzhe/Office-Word-MCP-Server | docx2typed backend (typed workdir + edit.md + sync + build) |
|---|---|---|
| State across calls | None — path-per-call, reopen+save each time | **Typed workdir** (immutable `_template.docx`, `typed.md`, `styles.json`, `format.json`) with `edit.md` agent projection + `edit.state.json` authoritative binding (`typed_docx.py:474-532` `extract_workdir`; `edit.py` Slice A header contract) |
| Edit surface for the agent | Runs/paragraphs by index/text; direct python-docx mutations | **`edit.md` span-free projection** with `<!--@p id=...-->` markers and `\u27e6token\u27e7` placeholders for non-text (`edit.py:render_edit_projection`) — agent never sees OOXML or run indexes |
| Style safety | Style by name with silent Normal fallback (`tools/content_tools.py:113-174`); direct run formatting can produce unanchored mixed-style results | **Hash-bound style registry** (`styles.json`, content-addressed canonical styles); sync assigns styles via explicit caret policy (left-context, right at offset 0, `insertion_style` for empty) and **rejects** unanchored mixed rewrites as `unanchored-mixed-rewrite` (`edit_sync.py` module docstring + `_assign_style`) |
| Freshness / drift | None — caller must track what changed; the file is the truth | **Four freshness states** clean/dirty/stale-clean/conflict; `validate`/`build`/`verify` reject every non-clean state; header/sidecar disagreement fails as `edit-header-tampered` (`edit.py` module docstring; `typed_docx.py:820-981` `validate_workdir`/`build_workdir` gates) |
| Atomicity | Ad hoc; `.tmp`+`os.replace` only in footnote path (`core/footnotes.py:453-475`) | **Transactional build**: writes temp DOCX, runs package checks + independent verification, then atomic publish (`SKILL.md` Verification contract; `typed_docx.py:983+` `build_workdir`) |
| Structural protection | Direct OOXML pokes (bullets via `OxmlElement('w:numPr')` `document_utils.py:298-336`; footnotes via zipfile+lxml `core/footnotes.py:283-475`) — capable but unguarded | **Locked structure**: paragraph IDs, base styles, token IDs, anchors, opaque refs are read-only; touching a protected paragraph fails before output (`SKILL.md` Typed editing rules; `typed_docx.py:630-683` `_validate_styles`/`_validate_cross_boundary_edit`/`package_guard`) |
| Error reporting | Human strings, exceptions flattened into prose (`tools/content_tools.py:107-110`) | **Structured ValidationError taxonomy** (`typed_docx.py:80-83`) — MCP layer can map these to typed `isError` results instead of string-parsing |

**Verdict:** the GongRzhe model is "the file is the session" — correct for a stateless CLI-ish server, wrong for a tool whose backend already has a transaction/verification discipline. docx2typed-mcp should expose **commands over the workdir**, not mutations over the file.

### What we should still copy from GongRzhe

1. **`filename`-first, explicit-parameter tool signatures with python type hints as the schema.** FastMCP derives input schemas from hints + docstrings (`main.py:96-109`), so no JSON-schema authoring. Our tools should be `workdir: str` + typed optionals, keeping the same zero-bootstrap ergonomics.
2. **Index-or-text dual addressing with TOC-skipping search** (`utils/document_utils.py:194-241`). The "insert near text, or by paragraph index" pattern is exactly what agents need and maps naturally onto our `<!--@p id=...-->` markers — but instead of returning an index we return/accept *paragraph IDs* (`P0`, `P1`, ...), which are stable across refresh in our model.
3. **The read-side tool set as a thin, complete inventory**: `get_document_info`, `get_document_text`, `get_document_outline`, `list_available_documents`, `get_document_xml` (`main.py:121-166`, `tools/document_tools.py:50-110, 212-215`). Our equivalents must read from the workdir (`view --mode clean/style/raw`) and add `edit status/refresh/sync` — read-only hints (`readOnlyHint=True`) reused verbatim.
4. **`output_filename` copy-first save semantics** (`core/footnotes.py:283-301`): our `build` already supports `-o output.docx`; the MCP tool should default to `output_filename` and treat the workdir as immutable, matching our "never mutate source workdir" rule.
5. **Pre-flight writeability/existence checks with actionable prose** (`utils/file_utils.py:9-44`): keep the *spirit* (fail early with "consider creating a copy") but return it as a structured error with code, not a string.
6. **Transport config via env with stdio default** (`main.py:30-62`): adopt verbatim for dev/stdio parity; keep `MCP_TRANSPORT` override for streamable-http.

### What the MCP layer should expose so the agent NEVER touches .docx internals

The workdir already isolates the agent from OOXML. The MCP layer's job is to make that isolation *complete and discoverable* — expose a command surface, not a python-docx surface:

- `workdir_open(workdir)` → returns `edit.md` text (or `view --mode clean`), freshness state, and the file inventory. First call of any session; the *only* entry point.
- `workdir_status(workdir)` → structured freshness (`clean|dirty|stale-clean|conflict`), template/source fingerprints, warnings. Agent calls before every build.
- `edit_apply(workdir, edit_text)` → writes the new `edit.md` draft and runs `edit sync`; returns per-hunk acceptance/warnings + new freshness state (not "paragraph 3 formatted"). Mirrors `docx2typed edit sync` (`SKILL.md` Clean edit state section).
- `build_docx(workdir, output_filename=None)` → `build_workdir` (validates, requires clean, atomic publish); returns output path + verification summary.
- `verify_output(workdir, output_docx)` → independent re-derivation check; returns pass/fail with diff of text/styles/structure.
- `paragraph_locate(workdir, query)` → maps a text search to stable paragraph IDs (index-or-text addressing, `document_utils.py:194-241` pattern) so later edit drafts reference IDs, never indexes.
- **Forbidden in the tool layer:** raw XML, run indexes, style-name strings applied ad hoc, "open document" handles, in-place save of the source file. Everything goes through `typed.md`/`edit.md` + `sync` + `build`.

Net: the GongRzhe server proves FastMCP + python-docx is a viable, widely-adopted shape; its weaknesses (stateless reopen, string errors, unguarded OOXML pokes, silent style fallback) are precisely the four things the docx2typed backend already solves. docx2typed-mcp = GongRzhe's ergonomics + docx2typed's transaction model.

---

## Appendix: exact citations used

- `main.py:30-62` — transport config (env, stdio default)
- `main.py:91-694` — `register_tools()`; 54 `@mcp.tool` defs; every tool `filename`-first
- `main.py:696-750` — `run_server()` / `main()`; `mcp.run(transport=...)`
- `tools/document_tools.py:14-48` — `create_document` (open-save, metadata)
- `tools/document_tools.py:50-110` — info/text/outline reads; `:212-215` XML
- `tools/content_tools.py:17-111` — `add_heading`: existence/writeability checks, style fallback, open-save
- `tools/content_tools.py:113-174` — `add_paragraph`: style-by-name + silent Normal fallback
- `tools/content_tools.py:395-429` — `delete_paragraph`: index bounds, lxml `p.getparent().remove(p)`
- `tools/content_tools.py:431-461` — `search_and_replace` (run-level replace via `find_and_replace_text`)
- `tools/format_tools.py:25-133` — `format_text`: index/text-position validation, run-splitting
- `utils/document_utils.py:13-39` — `get_document_properties` (error-dict returns)
- `utils/document_utils.py:138-178` — `find_and_replace_text` (run-level, TOC skip)
- `utils/document_utils.py:194-241` — `insert_header_near_text` (text-or-index anchor, `doc.save`)
- `utils/document_utils.py:298-336` — `add_bullet_numbering` (direct `w:numPr` OOXML)
- `utils/document_utils.py:441-530` — `delete_block_under_header` / `replace_paragraph_block_below_header`
- `utils/file_utils.py:9-44` — `check_file_writeable` (permission + lock probe)
- `utils/file_utils.py:73-81` — `ensure_docx_extension`
- `core/styles.py:8-35` — `ensure_heading_style`; `:53-130` — `create_style`
- `core/footnotes.py:283-475` — `add_footnote_robust`: tuple errors, copy-first output, zipfile+lxml parts editing, `.tmp`+`os.replace` atomic publish
- `pyproject.toml:20-27` — deps: python-docx, fastmcp, msoffcrypto-tool, docx2pdf
- `README.md:29-113` — feature list; `README.md:221-333` — API reference
- Runner-up: `word-mcp-live` `word_document_server/tools/live_tools.py:25-138` — COM state in Word app, `filename: str = None` = active document
