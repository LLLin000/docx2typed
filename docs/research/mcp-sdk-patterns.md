# MCP Python SDK — Server-Side Architecture Reference (for docx2typed-mcp)

Reference notes for building a document-editing MCP server whose backend is an
existing DOCX editing engine (Python, typed workdir + edit sync). Distilled
from the official SDK source, plus the official filesystem reference server.

## Repo and commit

- **modelcontextprotocol/python-sdk** @ `a4f4ccd` ("Link the released 2026-07-28 spec and point migrators at /v1/ (#3214)"), shallow clone. Main branch, on the **v2.0.0 release line** (latest tag `v2.0.0rc1`).
- **modelcontextprotocol/servers** @ `76d64c8` (filesystem server used as the path-confinement/error reference).

> ⚠️ Migration-critical fact: in this version the high-level class is
> **`MCPServer`**, imported from `mcp.server.mcpserver`. The name `FastMCP` no
> longer exists anywhere in `src/mcp/` — tutorials written against earlier
> releases are stale. Protocol support: handshake-era `2025-11-25` and
> "modern" `2026-07-28` (no handshake), served automatically by the SDK
> (`src/mcp-types/mcp_types/version.py:28-41`, `MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)`).

## Architecture (module layout, entrypoint, transport)

### Package layout (`src/mcp/`)

| Path | Role |
|---|---|
| `src/mcp/server/mcpserver/` | High-level decorator-based API: `MCPServer`, `Context`, `ToolManager`, `Tool` |
| `src/mcp/server/lowlevel/` | Low-level constructor-based `Server` (register handlers by method string) |
| `src/mcp/server/stdio.py` | stdio transport (`stdio_server()`) |
| `src/mcp/server/runner.py` | Connection run loop (`serve_dual_era_loop`), middleware chain, error mapping |
| `src/mcp/shared/` | `exceptions.py` (`MCPError`), `jsonrpc_dispatcher.py` (exception→ErrorData), `path_security.py` (`safe_join`), `tool_name_validation.py` |
| `src/mcp-types/mcp_types/` | Wire types (`ErrorData`, `CallToolResult`, ...) — `mcp.types` mirrors it (`src/mcp/types/__init__.py:30-31`) |

### Low-level entrypoint

`mcp.server.lowlevel.Server` is constructor-based: you pass `on_list_tools` /
`on_call_tool` handlers, each `async (ctx, params) -> result`, and params are
validated against a pydantic model you supply per method
(`src/mcp/server/lowlevel/server.py:460-495`, `add_request_handler(method, params_type, handler)`).
The module docstring gives the canonical stdio main
(`src/mcp/server/lowlevel/server.py:9-27`):

```python
async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
asyncio.run(main())
```

`Server.run()` enters the lifespan then drives the connection until the read
side closes (`src/mcp/server/lowlevel/server.py:691-711`). `initialize` is
reserved (runner-owned) — registering it raises `ValueError`
(`lowlevel/server.py:486-494`).

### High-level entrypoint (recommended for us)

`MCPServer` wraps the lowlevel `Server` and registers one `_handle_call_tool`
dispatcher (`src/mcp/server/mcpserver/server.py:415-424`); tools are plain
functions. Running over stdio is one call (`mcpserver/server.py:1018-1023`):

```python
async def run_stdio_async(self) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await self._lowlevel_server.run(
            read_stream, write_stream,
            self._lowlevel_server.create_initialization_options(),
        )
```

and the sync `run()` defaults to `transport="stdio"`, dispatching via
`anyio.run(self.run_stdio_async)` (`mcpserver/server.py:357-408`).

### Stdio framing details

`stdio_server()` is a context manager that **claims fd 0/1** for the wire: while
serving, fd 0 points at the null device and fd 1 at stderr, so stray `print()`
and child-process output never corrupt the protocol; descriptors are restored
on exit (`src/mcp/server/stdio.py:162-216`). Framing is JSONL — one JSON-RPC
message per line, dumped with `model_dump_json(by_alias=True, exclude_unset=True)`
(`stdio.py:203-206`), UTF-8 wrapped on both ends (`stdio.py:171-172`). A second
concurrent `stdio_server()` raises `RuntimeError` (`stdio.py:66-70` claim
contract). Windows handles are rebound explicitly (`stdio.py:114-117`,
`rebind_std_handle_to_fd`).

**Consequence for us:** never `print()` in server code — use `logging` to
stderr (the fd diversion makes stderr safe), or `ctx.info(...)`.

## Tool definition and validation patterns

### Declaration

- `@mcp.tool()` decorator — must be **called** (`@mcp.tool`, without parens, raises `TypeError`), `mcpserver/server.py:621-668`.
- `mcp.add_tool(fn, name=None, title=None, description=None, annotations=None, structured_output=None)`, `mcpserver/server.py:570-616`. `name` defaults to `fn.__name__`; lambdas rejected (`tools/base.py:75-77`).
- Tool names are validated (`validate_and_warn_tool_name`, `tools/base.py:139-141`).
- **Input schema is generated from type annotations + docstrings**, not hand-written: `func_metadata(fn, ...)` builds a pydantic `arg_model` from the signature and exposes `model_json_schema(by_alias=True)` as the tool's `input_schema` (`tools/base.py:95-101`). Docstring → `description`; `Annotated[str, Field(description=...)]` and defaults feed the JSON schema.
- A parameter annotated `Context` is auto-detected and **excluded from the schema** (`find_context_parameter`, `tools/base.py:150-153`).

```python
@mcp.tool()
async def edit_text(ctx: Context, path: str, start: int, end: int, text: str) -> str:
    """Replace the text in [start, end) with `text`.

    Args:
        path: workdir-relative path of the .docx
        start: 0-based char offset of the first char to replace
        ...
    """
    workdir = ctx.request_context.lifespan_context  # typed workdir
    ...
```

### Argument validation (trust boundary)

- `Tool.run` validates every call through the pydantic arg model before invoking the function: `validate_arguments` → `arg_model.model_validate(...)` (`utilities/func_metadata.py:72-79`, `tools/base.py:123-184`). A `ValidationError` surfaces to the client as `INVALID_PARAMS (-32602)` with message `"Invalid request parameters"` and **no pydantic text on the wire** (`src/mcp/shared/jsonrpc_dispatcher.py:88-103`).
- Sync tools run in a worker thread (`anyio.to_thread.run_sync`), async tools are awaited (`utilities/func_metadata.py:96-108`). Blocking file/engine IO in a **sync** tool therefore never blocks the event loop — safe and simpler.
- Result conversion: `str` → `TextContent`; lists flattened recursively; other objects JSON-encoded (`utilities/func_metadata.py:543-574`). `structured_output=True` additionally fills `CallToolResult.structuredContent`, validated against the return annotation's output schema (`func_metadata.py:110-144`) — the filesystem server always returns `{content, structuredContent}`.
- `ToolAnnotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) are declared per tool — the filesystem server marks `write_file` as `{readOnlyHint: false, idempotentHint: true, destructiveHint: true, openWorldHint: false}` (`servers/src/filesystem/index.ts:358-376`). Clients use these to gate destructive calls.

## State management and concurrency

### Lifespan = the workdir slot

Both layers take a `lifespan` async context manager; its yielded value becomes
`ctx.request_context.lifespan_context` for every request in the connection
(`mcpserver/server.py:148-260` wraps the user lifespan into the lowlevel one).
This is the natural home for a **typed Workdir** object (open once per server
process, cleaned up on exit) — mirroring how the docx2typed backend models a
typed workdir + edit sync.

### Sessions and request context

- Low-level: `ServerRequestContext` carries `request_id`, `session`, `meta`, `lifespan_context`, negotiated `protocol_version` (`runner.py:175-240` builds it per request).
- High-level: `Context` wraps it (`mcpserver/context.py:32-63`) and adds ergonomics: `ctx.report_progress(progress, total, message)` (`context.py:113-120`), deprecated logging `ctx.info/debug/warning/error` (`context.py:225+`), `ctx.read_resource(...)`, `ctx.elicit(...)`. `ctx.request_context.session` is the back-channel for server-initiated notifications.
- stdio = **one connection per process**; `Server.run` serves until the read side closes (`lowlevel/server.py:691-711`). Cross-connection concurrency is a non-issue on stdio.

### Concurrency within a connection

There is **no built-in serialization of concurrent tool calls** — that is the
server's job. The filesystem server's approach: writes are atomic
**temp-file + rename** (never in-place mutation), which sidesteps
read/write races entirely (`servers/src/filesystem/lib.ts:266-278`). For a
DOCX engine that is not thread-safe, the analogous pattern is an
`asyncio.Lock` (or serialized edit sync) held for the load-edit-save
transaction; the SDK does not provide one, so the lock belongs in the
workdir object. `Context` is request-scoped and must not be shared across
requests (`context.py:95-99` raises if used outside a request).

## Error handling

### The two error channels (crucial)

1. **Protocol errors** — JSON-RPC `ErrorData(code, message, data)`. Codes:
   `PARSE_ERROR -32700`, `INVALID_REQUEST -32600`, `METHOD_NOT_FOUND -32601`,
   `INVALID_PARAMS -32602`, `INTERNAL_ERROR -32603`
   (`src/mcp-types/mcp_types/jsonrpc.py:94-107`); the envelope
   (`jsonrpc.py:113-123`): `message` SHOULD be a concise single sentence,
   `data` is sender-defined.
2. **Tool results** — `CallToolResult(content=[...], is_error=True)`.

`_handle_call_tool` draws the line explicitly
(`mcpserver/server.py:415-424`):

```python
try:
    return await self.call_tool(params.name, params.arguments or {}, context)
except MCPError:
    raise                       # → JSON-RPC error response
except Exception as e:
    return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)
```

So: raise `MCPError(code, message, data)` (defined `src/mcp/shared/exceptions.py:19-38`,
`error: ErrorData`) when the *request itself* must fail at the protocol level;
return an `is_error=True` `CallToolResult` for *domain* failures (not found,
conflict, engine error) — the filesystem server throws plain `Error`s, which
the TS SDK turns into `is_error` results (e.g. "Access denied - path outside
allowed directories", `lib.ts:105-107`).

### Fallbacks the SDK enforces

- `MCPError` → its own `ErrorData` verbatim; pydantic `ValidationError` → `INVALID_PARAMS`; **anything else** on the modern path → logged server-side and surfaced as generic `INTERNAL_ERROR` "Internal server error" so handler internals never leak (`src/mcp/server/runner.py:534-550`, `modern_error_data`; shared ladder in `jsonrpc_dispatcher.py:88-103`).
- Tool-body exceptions get wrapped in `ToolError` (`tools/base.py:181-184`) before reaching the `is_error=True` path.
- `_dump_result` also treats a handler *returning* `ErrorData` as a protocol error (`runner.py:110-124`).

### Recommended mapping for docx2typed-mcp

| Condition | Channel |
|---|---|
| Malformed JSON / unknown tool / bad types (pydantic) | protocol: `INVALID_PARAMS` etc. (automatic) |
| Semantically invalid input (doc id unknown, index out of range, path escapes workdir) | `MCPError(code=INVALID_PARAMS, message=..., data={...})` — include the offending value in `data` |
| Engine/IO failure (corrupt docx, lock timeout, save conflict) | `CallToolResult(is_error=True)` with a human-readable message |
| Unexpected exception | let the SDK fallback produce `INTERNAL_ERROR` (never leak tracebacks) |

## Directly reusable patterns for docx2typed-mcp

### 1. Stdio server main() — copy verbatim (high-level)

```python
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("docx2typed", version="0.1.0")

if __name__ == "__main__":
    mcp.run()  # transport defaults to "stdio"
```
(`mcpserver/server.py:357-408`; quickstart shape in `examples/mcpserver/readme-quickstart.py:3-8`; everything-server shows the `transport=` kwarg style at `examples/servers/everything-server/mcp_everything_server/server.py:767-779`.)

### 2. Workdir confinement — use the SDK's `safe_join` instead of rolling our own

`mcp.shared.path_security.safe_join(base, *parts)` resolves `base`, rejects
null bytes and absolute parts, joins, re-resolves, and verifies
`target.is_relative_to(base)` — catching `..` traversal, absolute-path
injection, **and symlink escapes** (`src/mcp/shared/path_security.py:121-169`).
This is the Python equivalent of the filesystem server's
`validatePath` (`servers/src/filesystem/lib.ts:99-140`), whose full
confinement recipe is:

1. normalize (`path-utils.ts:39` normalizePath — strip quotes, collapse separators, expand `~`);
2. reject if not inside allowed roots (`path-validation.ts:11-85`: type checks, empty inputs, null bytes, `path.resolve` then prefix containment, Windows drive-root special case);
3. `fs.realpath` and re-check (symlink attack prevention, `lib.ts:116-121`);
4. for not-yet-existing files (write targets): realpath the **parent** and re-check (`lib.ts:123-135`);
5. allow-list is resolved once at startup and optionally replaced by MCP **roots** (`index.ts:27-84`, `roots-utils.ts:52-76`, `index.ts:724-770`).

For us: `workdir.path` is the base; every tool's `path` argument goes through
`safe_join(workdir.path, rel_path)` and the resolved path is what the engine
touches. Deny with `MCPError(code=INVALID_PARAMS, message="path escapes workdir", data={"path": ...})`.

### 3. Atomic document writes — temp file + rename

The filesystem server never mutates in place: read → transform in memory →
write `<target>.<hex>.tmp` → `rename` over the target, unlinking the temp on
failure (`servers/src/filesystem/lib.ts:266-278`). For DOCX this is doubly
right: the edit sync produces a new document state, so write the rebuilt
`.docx` to a temp sibling then `os.replace()` into place. `edit_file`'s
all-or-nothing loop (apply edits sequentially, throw on the first unmatched
`oldText` before writing anything, `lib.ts:196-264`) is the model for our
multi-op edits: **validate every op, then write once**.

### 4. Input schemas that self-document

Type-annotated signatures + docstrings → JSON schema automatically
(`tools/base.py:95-101`). Use `Literal["replace","insert","delete"]`,
`Annotated[int, Field(ge=0)]`, and per-arg docstrings so the LLM client sees
a precise contract. ~8 atomic tools (open/read/edit/append/delete/save/query
structure/export) fit naturally as module-level functions with `@mcp.tool()`.

### 5. Progress and notifications

Long edits: `await ctx.report_progress(i, n, "applying edit 3/8")`
(`mcpserver/context.py:113-120`); document changed → `await ctx.notify_tools_changed()`
if the tool list is dynamic (`context.py:129-133`). Both ride
`ctx.request_context.session` (`context.py:119-120`).

### 6. What NOT to build

- No JSON-RPC plumbing: the SDK owns framing, handshake (and the modern 2026-07-28 era via `serve_dual_era_loop`), middleware, and error mapping.
- No custom path-validation library: `safe_join` + the filesystem server's recipe covers traversal/symlink/absolute/null-byte cases; keep the docx-specific checks (document must exist, indices in range) in the tool layer as `MCPError`s.
- No print() to stdout — the fd-claim contract diverts it (`stdio.py:162-216`).

### Caveats

- `FastMCP` is renamed to `MCPServer` in v2.0.0; the logging capability is deprecated as of the 2026-07-28 spec (SEP-2577) — use `logging` to stderr, not `ctx.info`, for server-side diagnostics.
- `safe_join`'s symlink check is point-in-time; a tree modified concurrently could swap a directory for a symlink between check and open (`path_security.py:129-133`). For a single-user workdir this is acceptable; the SDK notes `O_NOFOLLOW` as the hardened alternative.
- `Context` is request-scoped — do not stash it on the workdir or reuse across calls.
