# patch_mcp — Safety Patterns for a Document-Editing MCP Server

Research note for `docx2typed-mcp` (an MCP server whose backend is an existing DOCX editing engine: typed workdir + edit sync with freshness hashes, staged publish, rollback). This file distills the safety architecture of `shenning00/patch_mcp` and maps every mechanism onto that backend.

## Repo and commit

- Repo: `https://github.com/shenning00/patch_mcp`
- Commit read (shallow clone): `0f4cef8146cdd00f8673c46e3c9eac3aebb37cb3`
  (Sun Oct 19 2025, "feat: Enhance apply_patch tool description for better LLM discoverability")
- Version: `2.0.0` (`pyproject.toml:5`), `requires-python = ">=3.10"`, deps `pydantic>=2.0.0`, `mcp>=0.1.0` (`pyproject.toml:17-19`)
- Transport: MCP stdio via the official Python SDK (`mcp.server.stdio.stdio_server`, `server.py:302-317`)

## Architecture (module layout, entrypoint, transport)

Single-package `src` layout; the server is a thin router over plain synchronous functions.

| Module | Role |
|---|---|
| `src/patch_mcp/server.py` | MCP server instance, `list_tools`, `call_tool` router, stdio `main()` |
| `src/patch_mcp/tools/apply.py` | `apply_patch` (validate → dry-run → temp+rename) |
| `src/patch_mcp/tools/validate.py` | `validate_patch` (read-only gate, preview, context check) |
| `src/patch_mcp/tools/revert.py` | `revert_patch` (content-inversion revert) |
| `src/patch_mcp/tools/generate.py` | `generate_patch` (difflib unified diff + sensitive-content scan) |
| `src/patch_mcp/tools/inspect.py` | `inspect_patch` (header/hunk parser, multi-file analysis, no file needed) |
| `src/patch_mcp/tools/backup.py` | `backup_file` / `restore_backup` (timestamped copies) |
| `src/patch_mcp/utils.py` | `validate_file_safety`, `is_binary_file`, `atomic_file_replace`, `sanitize_error_message`, `check_path_traversal`, `detect_sensitive_content` |
| `src/patch_mcp/models.py` | `ErrorType` enum + Pydantic result models |
| `src/patch_mcp/recovery.py`, `workflows.py` | Library-only recovery/workflow patterns (NOT exposed as MCP tools) |

Entrypoint: `server = Server("patch-mcp")` (`server.py:27`); `main()` runs `server.run(read_stream, write_stream, server.create_initialization_options())` over stdio (`server.py:302-317`). Seven tools are registered in `list_tools` (`server.py:31-193`): `apply_patch`, `validate_patch`, `revert_patch`, `generate_patch`, `inspect_patch`, `backup_file`, `restore_backup`.

Routing: `call_tool(name, arguments)` dispatches by name to the synchronous tool function and returns a single `TextContent` with `json.dumps(result, indent=2)` (`server.py:249-299`). Two consequences for our design:

1. **The MCP layer is a thin JSON envelope.** All semantics live in plain functions returning dicts; there is no `isError`/structured-error machinery — `success: false` is data, not a protocol error (`server.py:299`).
2. **Tool bodies are synchronous and run inline in the async `call_tool`.** No `asyncio.to_thread` anywhere — a slow tool blocks the event loop. docx2typed-mcp (DOCX ops are heavier) should offload with `asyncio.to_thread` or FastMCP's executor.

## Tool definition and validation patterns

**Declaration:** plain `mcp.types.Tool` objects with hand-written JSON Schema `inputSchema` dicts — no Pydantic function-calling layer. E.g. `apply_patch` declares `file_path`, `patch`, and optional `dry_run` with `"default": false` *in the description string* (`server.py:38-79`); the default is actually applied at dispatch time via `arguments.get("dry_run", False)` (`server.py:265-268`), not by the schema. `required` arrays list mandatory args (`server.py:78-79`).

**Input validation** is therefore two-tier: schema for presence/type, then code for everything else (file existence, patch format, context). Unknown tool name raises `ValueError` (`server.py:295-297`).

**Result contract:** every tool returns a dict with `success: bool`; success payloads carry domain fields (`changes`, `preview`, `backup_file`); failures carry `error: str` + `error_type: str`. The `ErrorType` enum (`models.py:13-43`) is the closed vocabulary — 6 standard codes (`file_not_found`, `permission_denied`, `invalid_patch`, `context_mismatch`, `encoding_error`, `io_error`) and 4 security codes (`symlink_error`, `binary_file`, `disk_space_error`, `resource_limit`).

**validate_patch return semantics are explicitly documented as CRITICAL** (`validate.py:19-80`):
- `success=True` + `valid=True` + `can_apply=True` → cleanly applicable, with `preview`.
- `success=False` + `valid=True` + `can_apply=False` + `reason` + `error_type="context_mismatch"` → *valid format, wrong file state* — deliberately NOT `success=True` (`validate.py:150-170`).
- `success=False` + `valid=False` + `error` + `error_type="invalid_patch"` → malformed patch (`validate.py:120-132`).

**Preview** (`validate.py:141-146`) is the dry-run currency: `lines_to_add`, `lines_to_remove`, `hunks`, `affected_line_range {start,end}` computed from hunk target ranges (`validate.py:134-137`). `apply_patch` reuses it to report `changes` on both dry-run and real apply (`apply.py:99-104`).

## State management and concurrency

**Fully stateless.** No session state, no workdir, no locks, no per-call IDs; `file_path` is passed on every call. The only "state" is the filesystem itself.

Concurrency properties and hazards:

- **Temp files are collision-free by construction:** `tempfile.mkstemp(dir=path.parent, prefix=".patch_tmp_", suffix=".tmp")` (`apply.py:125-127`) creates a unique file in the *same directory* as the target, which makes the follow-up rename same-filesystem and therefore atomic-ish (`utils.py:207-233`).
- **Atomicity differs per OS:** Unix `source.rename(target)` is atomic (`utils.py:232-233`); Windows must `target.unlink()` first, so there is a window where the target does not exist and a crash leaves no file (`utils.py:226-230`). The code comments this ("not atomic, but best we can do").
- **Validate→apply TOCTOU gap (the important one for us):** `apply_patch` calls `validate_patch` (which reads the file at `validate.py:94-99`), then later *re-reads* the file and applies by line number (`apply.py:113-118`). Nothing compares the two reads — no mtime, no hash. If the file changes between validate and apply (or two applies race), the second write blindly replaces `source_count` lines at `source_start` (`apply.py:287-290`) and the last rename wins. patch_mcp accepts this; a sync engine with **freshness hashes closes exactly this hole**.
- **restore_backup mtime check warns, does not block:** if the target was modified after the backup and `force=False`, it only appends a warning string to the message (`backup.py:310-327`); the restore still proceeds. Any pre-condition that must *block* has to be enforced by the caller.

## Error handling (structured errors, partial failure, atomicity)

**Structured dict errors, one shape:** `{"success": false, "file_path": ..., "error": ..., "error_type": ...}`. Exceptions are caught and mapped to codes: `UnicodeDecodeError → encoding_error`, `OSError → io_error`, bare `Exception → io_error` (`apply.py:151-175`); the read path in validate maps the same way (`validate.py:100-119`).

**Message sanitization is load-bearing:** context-mismatch reasons embed file content, so `sanitize_error_message` truncates quoted snippets >50 chars to `'[CONTENT]'` and strips absolute paths, keeping only filenames (`utils.py:236-278`; used at `validate.py:331-348`). This is the prompt-injection / content-leak guard for error text returned to the LLM — we should copy it verbatim.

**Atomicity model:**
- Single-file apply: validate-all → write temp → rename. Either the whole patch lands or nothing does; multi-hunk patches are covered by the single rename (`apply.py:113-135`), so "multi-hunk atomic" is real.
- Temp cleanup on failure: `except Exception: temp_file.unlink()` then re-raise (`apply.py:137-140`); test asserts no `.patch_tmp_*` residue (`tests/test_apply.py:352-378`).

**Partial failure / rollback patterns live in `recovery.py` (library only, not tools):**
- `safe_apply_with_backup` (`recovery.py:24-123`): backup → validate → apply → on apply failure `restore_backup` (`recovery.py:100-119`); backup is always created and its path always returned.
- `batch_apply_patches` (`recovery.py:235-384`): **backup all files first** (`recovery.py:294-314`), apply sequentially, and on first failure **roll back ALL** by restoring every backup (`recovery.py:340-366`). This is the all-or-nothing multi-file transaction we want for multi-edit tools.
- `validate_before_apply` (`recovery.py:131-233`): inspect format → validate can-apply → (dry-run or apply).
- `workflows.py` adds `apply_patches_with_revert` (sequential + revert-all on failure, raising if a revert itself fails), `apply_patch_with_backup` (with emergency restore on unexpected exception), `apply_patches_atomic`, and a progressive-validation helper that runs `validate_file_safety(path, check_write=True, check_space=True)` as step 0 (`workflows.py:475-479`).

## Directly reusable patterns for docx2typed-mcp

### The exact validation-before-apply sequence (from `apply_patch`, `apply.py:75-135`)

1. **Safety gate:** `validate_file_safety(path, check_write=not dry_run, check_space=not dry_run)` (`apply.py:75`) — exists / regular file / **not a symlink (always rejected, security policy)** / not binary / ≤10 MB (`utils.py:21-115`; binary heuristic = null bytes + UTF-8 decode + >30% non-text in first 8 KB, `utils.py:118-168`). Dry-run skips write and disk-space checks so a read-only file can still be previewed (`tests/test_apply.py:309-327`).
2. **Read-only validation:** `validate_patch` re-runs the safety gate, reads UTF-8, parses format, checks can-apply, builds preview (`validate.py:88-146`).
3. **Gate on failure:** any failure returns `success=False` before any write (`apply.py:87-97`).
4. **Dry-run short-circuit:** returns `success=True, applied=True` + preview stats, no mutation (`apply.py:106-111`; tests assert byte-identical file: `tests/test_apply.py:113-134`).
5. **Fresh read + blind positional apply:** re-read file, `_apply_patch_to_lines` (hunks sorted by `source_start` descending so line numbers stay valid, `apply.py:201-206`), `mkstemp` in target dir, write, `atomic_file_replace` (`apply.py:113-135`).
6. **Exception → structured error** (`apply.py:151-175`).

### Failure semantics (what happens on stale context / no-op / partial hunks)

- **Stale context:** `_can_apply_patch` checks (a) the hunk's `source_start..source_count` range is inside the file (`validate.py:306-318`) and (b) **each `-` removed line is a *member* of the affected slice** (`validate.py:320-348`). Two weaknesses to know: **context lines are never compared** (they are collected in `_parse_patch` but unused by `_can_apply_patch`), and the removed-line check is a membership test, not a positional match. A drifted file can pass validation if the removed lines still exist somewhere in the slice; apply then replaces `source_count` lines at the header position regardless (`apply.py:287-290`). `difflib.get_close_matches` only improves the error message (`validate.py:328-348`). Tests pin the intended behavior (partial context → `context_mismatch`, `tests/test_validate.py:224-244`; whitespace-sensitive comparison, `tests/test_validate.py:345-364`).
- **No-op (empty patch):** empty/whitespace patch parses as valid with 0 hunks (`validate.py:198-204`), `_can_apply_patch` returns `can_apply=True` immediately (`validate.py:301-302`), and apply "succeeds" with zero counts (`tests/test_apply.py:136-151`) — but still writes the file through temp+rename (mtime churn). Revert of an empty patch also succeeds with 0 hunks (`tests/test_revert.py:228-241`).
- **Partial hunks:** validation is all-or-nothing — the first failing hunk aborts with `can_apply=False` (`validate.py:304-350`); multi-hunk apply either lands entirely (one rename) or not at all (`apply.py:113-135`, `tests/test_apply.py:213-245`).
- **Revert:** `revert_patch` is *content-derived*, not state-derived: it textually reverses the patch (swap `+`/`-`, swap source/target ranges in `@@` headers — `revert.py:101-140`) and re-runs `apply_patch` on the reversed text (`revert.py:65-68`). It inherits all validation+atomicity, and if the file was edited in the affected areas it fails with `context_mismatch` reworded to "file has been modified since patch was applied" (`revert.py:86-92`, `tests/test_revert.py:59-87`). **There is no journal** — you must supply the exact original patch, and double-revert fails (`tests/test_revert.py:105-133`).

### Mapping to our sync engine (freshness hashes, staged publish, rollback)

| patch_mcp mechanism (citation) | Our backend equivalent | What the MCP layer must add |
|---|---|---|
| `validate_patch` read-only gate + preview (`validate.py:19-146`) | Sync snapshot + edit preview | **`preview_edit` / dry-run tool** — first-class read-only tool so the LLM inspects before mutating; also proves "validate" belongs in the protocol surface, not only inside apply |
| Validate-then-apply in one tool (`apply.py:75-97`) | Staged publish | Mutating tool must internally run validate → publish; never a bare publish |
| — missing: no freshness check between validate's read and apply's read (`apply.py:110-118` re-reads blindly) | **Freshness hashes** (this is the gap our engine already closes) | Re-check the workdir hash at publish time against the hash captured at preview/validate time; on mismatch return a structured error (new `error_type`, e.g. `freshness_mismatch`), like `context_mismatch` but *positioned* |
| temp-in-same-dir + rename (`apply.py:125-135`, `utils.py:207-233`) | Staged publish (write temp docx in workdir, atomically swap) | Same pattern; note the Windows caveat: `unlink+rename` is not atomic (`utils.py:226-230`) — on Windows keep the prior file until the rename succeeds, or accept the window and rely on rollback copies |
| `revert_patch` content inversion, needs exact original patch (`revert.py:16-140`) | Operation log / versioned snapshots | **`revert_edit` tool bound to an edit id or snapshot**, not the patch text; verify the current freshness hash before reverting; report `success/reverted/changes` in the same dict shape (`revert.py:71-84`) |
| `backup_file`/`restore_backup` timestamped copies (`backup.py:18-441`) | Rollback snapshots | Optional `backup` tool; note restore warns-but-proceeds on modified target (`backup.py:310-327`) — for us a modified workdir must *block* revert, which is the freshness check again |
| `safe_apply_with_backup` / `batch_apply_patches` backup-all → rollback-all (`recovery.py:24-123, 235-384`) | Staged multi-edit transaction | A multi-edit tool: run per-file freshness checks for **all** files before committing the batch (validate-all-then-publish), roll back every file if any publish fails — this is the pattern's whole point |
| Structured `{success, error, error_type}` + closed enum (`models.py:13-43`, `server.py:299`) | — | Adopt the same contract: every tool returns JSON text with `success` + `error_type`; keep the enum closed and documented; serialize with `json.dumps(indent=2)` into one `TextContent` |
| `sanitize_error_message` on content-bearing errors (`utils.py:236-278`) | — | Mandatory for DOCX text errors (we return document snippets): truncate quoted content >50 chars, strip absolute paths |
| `check_path_traversal` (`utils.py:171-205`) | — | Defined but **never wired into any tool** in patch_mcp (only tests) — the server trusts the client's paths. We must actually enforce a base-dir/typed-workdir jail at the tool boundary; a typed workdir already gives us the confinement point |

### What the MCP layer must add (explicit list)

1. **Dry-run preview tool** (`preview_edit`): read-only, returns affected range + change stats before any mutation — the direct analogue of `validate_patch`'s preview (`validate.py:141-146`), exposed as a separate tool rather than a boolean flag.
2. **Revert tool** (`revert_edit`): improved over patch_mcp — bound to an edit id/snapshot from our operation log instead of requiring the original patch text (`revert.py:65-68` re-applies reversed text; we can do better with state), and freshness-checked before acting.
3. **Freshness-hash enforcement between preview and publish** — the one safety mechanism patch_mcp lacks (`apply.py:110-118` TOCTOU); our sync engine's hashes make it free, and it upgrades `context_mismatch` into a *positioned* stale-state error.
4. (Optional) **`backup`/`restore` tools** and a **multi-edit atomic tool** following `batch_apply_patches` (`recovery.py:235-384`), with all freshness checks done before any publish.
5. **Async offloading** — run the DOCX engine in a thread/executor; patch_mcp's sync-in-async pattern (`server.py:264-299`) does not scale to document-sized work.

## Sources

Primary: `src/patch_mcp/{server,utils,models,recovery,workflows}.py`, `src/patch_mcp/tools/{apply,validate,revert,generate,inspect,backup}.py` at commit `0f4cef8146cdd00f8673c46e3c9eac3aebb37cb3`; tests under `tests/` (semantics pinned in `test_apply.py`, `test_validate.py`, `test_revert.py`, `test_security.py`). All line numbers cited above are from that commit.
