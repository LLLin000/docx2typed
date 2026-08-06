# Document MCP Server Patterns — from zavora-ai/mcp-document

Research note for designing **docx2typed-mcp**: a document-editing MCP server whose
backend is an existing Python DOCX editing engine (typed workdir + edit sync).
This file distills the architecture, tool vocabulary, and result/error envelope
conventions of the mature Rust MCP server `zavora-ai/mcp-document`, and proposes a
paragraph-scoped tool list for docx2typed-mcp grounded in what that repo does.

---

## Repo and commit

- **Repo**: https://github.com/zavora-ai/mcp-document (Apache-2.0, crate `mcp-document` 2.0.1)
- **Commit read (shallow clone, depth 1)**: `8a7de2226f3f3abdb1cbf832b193f7c3e828e126`
  ("fix: add lib.rs for docs.rs", 2026-05-27)
- **Language / SDK**: Rust 2024 edition; `rmcp` 1.7 (MCP server + stdio transport +
  `tool`/`tool_router` macros) and `adk-mcp-sdk` 0.1 (health check) — `src/server.rs:1-2`, `Cargo.toml`
- **Scale**: 6 source files, 623 lines total. 29 tools over 3 feature-flagged backends
  (12 Google, 10 Notion, 7 Microsoft — plus `notion_update_page` listed in `mcp-server.toml`).

## Architecture (module layout, entrypoint, transport)

**Layout** (one file per backend + one server file):

| File | Role |
|------|------|
| `src/main.rs` | Entrypoint: env-var-driven backend construction, stdio serve loop |
| `src/server.rs` | `DocumentServer` struct + all 29 `#[tool]` handlers + input types + HealthCheck |
| `src/google.rs` | `GoogleBackend` — raw `reqwest` calls to Google Docs/Drive REST APIs |
| `src/notion.rs` | `NotionBackend` — raw `reqwest` calls to Notion API |
| `src/microsoft.rs` | `MicrosoftBackend` — raw `reqwest` calls to MS Graph API |
| `mcp-server.toml` | Out-of-band governance manifest (risk classes, approval gates, credential bindings) |

**Entrypoint** (`src/main.rs`): read env vars `GOOGLE_DOCS_TOKEN`/`GOOGLE_ACCESS_TOKEN`,
`NOTION_API_KEY`, `MS_GRAPH_TOKEN` (lines 18-29); if none set, `anyhow::bail!` at startup
(lines 31-32). Each present backend is wrapped in `Arc` and stored in the server struct;
the server is then served on stdio: `server.serve(stdio()).await?` + `service.waiting()`
(lines 37-38). Transport is **stdio only** (`mcp-server.toml:8`). Feature flags
(`google`, `notion`, `microsoft`, `all-backends`) are compile-time only and gate the
*client crates* (`Cargo.toml`), not the tool registration.

**Key architectural fact — there is NO backend trait.** Each backend is a concrete
`#[derive(Clone)]` struct holding an HTTP client + credential (`src/google.rs:21-23`,
`src/notion.rs:13`, `src/microsoft.rs:13`), exposing inherent `async fn` methods returning
`anyhow::Result<...>`. The server holds one `Option<Arc<Backend>>` field per backend
(`src/server.rs:65-68`), so "configured" == "env var present" == "field is `Some`".

### How the same operation maps across backends

The repo does **not** dispatch one tool name to multiple backends. Instead the *operation*
is replicated per backend with a **prefixed tool name** and a per-backend input struct:

| Operation | Google | Notion | Microsoft |
|-----------|--------|--------|-----------|
| list | `google_list_docs` (server.rs:74) | `notion_list_pages` (server.rs:128) | `ms_list_docs` (server.rs:171) |
| search | `google_search_docs` (server.rs:78) | `notion_search` (server.rs:124) | `ms_search_docs` (server.rs:175) |
| get meta/structure | `google_get_doc` (server.rs:82) | `notion_get_page` (server.rs:132) | — |
| get content/text | `google_get_text` (server.rs:86) | `notion_get_content` (server.rs:136) | `ms_get_content` (server.rs:179) |
| create | `google_create_doc` (server.rs:90) | `notion_create_page` (server.rs:140) | `ms_create_doc` (server.rs:183) |
| positional insert/append | `google_insert_text` (server.rs:94) | `notion_append_blocks` (server.rs:144) | — |
| find/replace | `google_replace_text` (server.rs:98) | — | — |
| update | — | `notion_update_page` (server.rs:165) | `ms_update_doc` (server.rs:187) |
| delete / soft-delete | `google_delete_doc` (server.rs:118) | `notion_archive_page` (server.rs:148) | `ms_delete_doc` (server.rs:191) |
| comment (list/add) | `google_list_comments` / `google_add_comment` (server.rs:106,110) | `notion_list_comments` / `notion_add_comment` (server.rs:156,160) | — |
| share | `google_share_doc` (server.rs:114) | — | `ms_share_doc` (server.rs:195) |
| export | `google_export_doc` (server.rs:102) | — | — |
| query (DB) | — | `notion_query_database` (server.rs:152) | — |

Each tool body is a three-branch `match` over its own backend field:
`Some(g) => match g.method(...)` → `Ok`/`Err`, `None => "<Backend> backend not configured"`
(e.g. `src/server.rs:75-77`). Consequences for us:
1. **Backend identity lives in the tool name prefix**, so a client never has to choose a
   backend; it just calls `google_*` vs `notion_*`.
2. **Input structs are duplicated per backend** even when fields are identical
   (`DocIdInput` server.rs:16 vs `MsItemInput` server.rs:51 vs `NotionPageIdInput` server.rs:33).
3. For docx2typed-mcp (one backend, one engine) the multi-backend prefix machinery is
   overkill — one flat tool namespace with a shared `doc_id`/`workdir` reference is enough.

## Tool definition and validation patterns

**Declaration**: `#[tool(description = "...")]` attribute macro from `rmcp`, applied to
methods inside a `#[tool_router(server_handler)]` impl block (`src/server.rs:2,71,74`).
The macro registers the tool name (method name), the description, and the generated JSON
Schema for inputs.

**Input schemas**: every tool takes exactly one argument: `Parameters(InputStruct)` where
the struct derives `serde::Deserialize + schemars::JsonSchema` (`src/server.rs:12-55`).
`schemars` turns the Rust type into the MCP `inputSchema`. Conventions visible in the code:

- **Required vs optional** = plain field vs `#[serde(default = "fn")]`
  (`ListDocsInput` server.rs:12, `SearchInput` server.rs:14, `InsertTextInput` server.rs:20).
- **Defaults as helper fns**: `d20() -> 20`, `d1() -> 1`, `d_text() -> "text/plain"`,
  `d_reader() -> "reader"` (server.rs:57-60).
- **Enum-like freedom via `String` + doc comment**: role is `String` with the allowed
  values spelled out in a `///` doc comment (`/// "reader", "writer", or "commenter"`,
  server.rs:29-30); export format likewise (`/// "text/plain", "text/html", or "application/pdf"`,
  server.rs:24-25). Doc comments become JSON Schema descriptions — cheap, but there is
  **no runtime validation** that the value is one of the listed options.
- **Opaque passthrough for rich data**: Notion `properties` and database `filter` are
  `serde_json::Value` — schema says "JSON object", semantics live in the doc comment
  (server.rs:39,43-44).
- **No validation layer**: no "doc exists" pre-checks, no auth checks at tool level
  (auth is a stored token), no semantic validation of arguments. The remote API is the
  only validator, surfaced as `format!("Error: {e}")`.

**Error returns**: all tools return `String` (no `Result`, no `isError` JSON-RPC flag).
Errors are stringly-typed: `format!("Error: {e}")` (server.rs:76, etc.). Unconfigured
backend returns a literal string (server.rs:77). This is the repo's weakest convention —
see Error handling below.

## State management and concurrency

- **Stateless per call.** Each backend method performs an independent HTTP round trip
  against the remote API using the credential stored in the struct
  (`src/google.rs:31-38`, `src/notion.rs:19-43`, `src/microsoft.rs:23-34`). There is no
  workdir, no session, no cursor, no cached document model, no dirty state.
- **No locks.** Backends are `Clone` and shared behind `Arc` (server.rs:65-68, main.rs:21),
  so concurrent tool calls are safe *only because* each call is a self-contained request.
  There is no transaction, no per-document lock, and no last-writer-wins protection —
  two concurrent `insert_text` calls race at the remote API (Google Docs serializes them;
  this repo relies on the provider).
- **Read vs write separation** is *declarative, not runtime*: `mcp-server.toml` assigns
  each tool `risk_class = "read_only" | "internal_write" | "external_write"` and
  `requires_approval` (lines 18-19, 25-26, 46...). Reads (`list/search/get/export/query/
  comments`) are `read_only` + no approval; document-mutating writes
  (`create/insert/replace/append/update`) are `internal_write` with approval; side-effect
  or destructive ops (`share`, `delete`, `archive`) are `external_write` + approval. In
  the Rust code there is no distinction — every handler has the same shape; the risk
  classification lives beside the tool registry in TOML as governance metadata.
- **Health check**: `HealthCheck` trait impl returns `healthy: true` unconditionally
  (server.rs:202-206) — a liveness probe, not a backend reachability probe.

## Error handling (structured errors, partial failure, atomicity)

This is where mcp-document is **deliberately minimal**, and where docx2typed-mcp should
do better. Observed conventions:

1. **One error channel: the returned string.** `Ok(())` → human ack string
   (`"Created: {id}"` server.rs:92, `"Text inserted"` server.rs:96, `"Replaced"`
   server.rs:99, `"Deleted"` server.rs:121); `Err(e)` → `"Error: {e}"`.
2. **Success payloads are pretty-printed JSON strings** for structured results
   (`serde_json::to_string_pretty(&v)`, server.rs:76,84, etc.) and **raw strings** for
   text (`google_get_text` returns the body verbatim, server.rs:88; `ms_get_content`
   server.rs:181). So a client cannot tell from the JSON-RPC envelope whether the tool
   succeeded — it must parse the string. Mixed envelope: JSON documents, ack sentences,
   and errors all come back as `String` with no `isError`/structured discriminator.
3. **No partial failure / atomicity.** Notion `append_blocks` builds *all* blocks from the
   markdown in one PATCH (src/notion.rs:91-100) — all-or-nothing at the HTTP level, but a
   multi-request operation (e.g. search → get, or any future batch) has no rollback story.
   Google `replace_text` is a single `batchUpdate` (src/google.rs:88-93), the provider
   guarantees atomicity of the one request.
4. **No idempotency keys, no version checks, no conflict detection.** Update semantics are
   blind overwrites (`ms_update_doc` PUTs the whole content, src/microsoft.rs:66-73).
5. **Deletion is backend-dependent**: Notion archive is a soft delete (`"archived": true`,
   src/notion.rs:86-89), Google delete is hard (src/google.rs:135-139). The MCP surface
   hides this difference — the client cannot distinguish reversible from irreversible.

**Envelope conventions we extract** (contract for our own server, explicitly *not* copied
verbatim where it is weak):

| Concern | mcp-document convention | Verdict for docx2typed-mcp |
|---------|-------------------------|----------------------------|
| Result payload | string (pretty JSON for lists, ack text for writes, raw text for text reads) | Keep: JSON for structured reads; **add** `isError: false` envelope |
| Error | `"Error: {e}"` string | Replace: structured `{ error: { code, message, retryable } }` + JSON-RPC `isError` |
| Unconfigured resource | `"<Backend> backend not configured"` literal | Replace: typed error `workdir_not_found` |
| Write ack | prose strings (`"Text inserted"`) | Replace: `{ ok: true, applied: n, doc_id, rev }` |
| Atomicity | one HTTP request per tool = atomic by provider | Keep, but make `commit_sync` the single write barrier |
| Idempotency | none | Add: `revision`/edit-sync token on writes; `revert` is explicit undo |

## Directly reusable patterns for docx2typed-mcp (concrete, with citations)

1. **Tool name = operation, doc_id-first inputs.** Every mutating Google tool leads with
   `doc_id` (`DocIdInput`, `InsertTextInput`, `ReplaceTextInput` — server.rs:16-22); the
   id is the first positional concept. For us: every tool leads with `doc_id` (or the
   typed-workdir reference), matching the engine's typed-workdir model.
2. **Schema from types, not hand-written JSON.** Deriving `JsonSchema` from input structs
   (server.rs:12, 71) is the Python-side equivalent of Pydantic models on `FastMCP`
   tools — declare `@mcp.tool()` with typed args and let the SDK emit `inputSchema`.
   Copy the defaults-via-annotations habit (`limit: int = 20`) and keep enum-ish string
   params with explicit allowed-value docs (server.rs:24-25, 29-30).
3. **Positional insert semantics already exist** — `google_insert_text(doc_id, text, index)`
   (server.rs:20, src/google.rs:81-86) is a direct precedent for `insert_paragraph` and
   for `move_paragraph` = `insert` at target index + `delete` at source index (the repo
   has no move tool; Google's own API composes it the same way).
4. **Find/replace is `matchCase`-aware and documented** (`ReplaceTextInput`,
   src/google.rs:88-93) — precedent for `replace_text`; we should add `match_case`/
   `regex` options the repo lacks.
5. **Delete vs soft-delete naming** (hard `google_delete_doc` vs `notion_archive_page`,
   server.rs:118 vs 148): for docx2typed-mcp, `revert` is the soft-delete analog — it must
   never be a hard destroy; `commit_sync` writes forward only.
6. **Read/write split via explicit registry metadata** (mcp-server.toml:18-19,46...):
   even though our runtime code won't need approval gates, we SHOULD keep a per-tool
   `risk_class` column (read_only / internal_write / destructive) in the tool list below
   so a future guardrail layer can consume it without touching handlers.
7. **Export-as-derivative-read pattern** (`google_export_doc` returns bytes/base64,
   server.rs:102-105) — precedent for `diff_preview`: a *read* tool that materializes a
   derived artifact (unified diff of pending edits) rather than mutating state.
8. **Health/validation as a first-class check** (server.rs:202-206) — precedent for
   `validate_document`: a read-only liveness/structure probe, but unlike the repo's
   unconditional `healthy: true`, ours should run real structural checks on the docx.
9. **What NOT to copy**: stringly-typed errors (`format!("Error: {e}")`, server.rs:76),
   ack-prose write results (server.rs:92,96), mixed success envelopes, no validation
   layer, no concurrency story. docx2typed-mcp's edit-sync engine demands a structured
   envelope, a single-writer lock per doc_id, and revision-tagged commits.

## Contract structure for docx2typed-mcp

### Tool vocabulary a document MCP server needs (from the 29-tool surface)

The repo's vocabulary reduces to 12 operations; docx2typed-mcp needs a paragraph-scoped
subset of exactly 5 of them, plus 3 document-level ops this repo lacks:

| Operation | In mcp-document | docx2typed-mcp mapping |
|-----------|-----------------|------------------------|
| list | `*_list_*` (server.rs:74,128,171) | `list_paragraphs` |
| get (scoped read) | `*_get_*` (server.rs:82,86,132,136,179) | `get_paragraph` |
| replace | `google_replace_text` (server.rs:98) | `replace_text` |
| insert (positional) | `google_insert_text` (server.rs:94) | `insert_paragraph` |
| delete | `google_delete_doc` / `notion_archive_page` (server.rs:118,148) | `delete_paragraph` |
| move | *(compose insert+delete)* | `move_paragraph` |
| validate | *(only HealthCheck, server.rs:202)* | `validate_document` |
| diff / export | `google_export_doc` (server.rs:102) | `diff_preview` |
| commit (update barrier) | `ms_update_doc` / `notion_update_page` (server.rs:187,165) | `commit_sync` |
| revert | *(notion archive ≈ reversible delete)* | `revert` |

### Proposed tool list for docx2typed-mcp (paragraph-scoped)

Conventions: every tool takes `doc_id` first; writes are staged on the typed workdir and
only `commit_sync` persists to the real file; all results return a structured JSON object
(`{"ok": true, ...}` or `{"ok": false, "error": {...}}`).

| Tool | Semantics (one line) | Risk class |
|------|----------------------|------------|
| `get_paragraph(doc_id, paragraph_id)` | Read one paragraph: text, style, and position metadata from the workdir model. | read_only |
| `list_paragraphs(doc_id, offset?, limit?)` | Enumerate paragraphs with stable ids, headings, and text previews (list-scope analog of `*_list_*`). | read_only |
| `replace_text(doc_id, find, replace, match_case?, regex?)` | Find/replace within paragraph text (or document-wide if `scope=all`), staged as pending edit. | internal_write |
| `insert_paragraph(doc_id, after_paragraph_id?, text, style?)` | Insert a new paragraph after `after_paragraph_id` (or append), positional analog of `google_insert_text`. | internal_write |
| `delete_paragraph(doc_id, paragraph_id)` | Remove a paragraph from the workdir model — reversible until `commit_sync`. | destructive (staged) |
| `move_paragraph(doc_id, paragraph_id, after_paragraph_id)` | Reorder a paragraph by re-parenting it after a target — implemented as staged insert+delete, like Google's API. | internal_write |
| `validate_document(doc_id)` | Run structural checks (well-formed XML, style refs, bookmark integrity) on the workdir copy; returns issues, mutates nothing. | read_only |
| `diff_preview(doc_id)` | Render a unified diff of staged edits vs the last committed revision; read-only, never touches the file. | read_only |
| `commit_sync(doc_id)` | Apply staged edits to the real docx and bump the revision — the single write barrier, analog of `ms_update_doc`'s blind PUT but revision-tagged. | internal_write (approval-worthy) |
| `revert(doc_id, revision?)` | Discard staged edits (or restore a prior revision) — soft, reversible operation, analog of Notion's archive-then-restore. | destructive (reversible) |

Rationale grounded in the repo: 5 of the 10 map 1:1 onto existing tools (`get/list/
replace/insert/delete`); `move_paragraph` follows the repo's only positional-write
primitive (insert at index, src/google.rs:81-86) composed with delete;
`validate_document` and `diff_preview` follow the repo's read-only probe/derivative-read
patterns (HealthCheck server.rs:202-206; export server.rs:102-105); `commit_sync` and
`revert` replace the repo's weak write semantics (prose ack server.rs:96/99, hard-delete
src/google.rs:135-139) with an explicit, reversible, revision-tagged write barrier.
