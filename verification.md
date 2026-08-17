# verification.md — acceptance gates

Every document workflow ends on these gates. Every claim about an output must
name the command or host check that proved it.

## End-to-end seam

```text
source DOCX → extract workdir → inspect/enumerate → edit or review → build output DOCX → independent verify
```

`verify` and MCP `verify_output` re-derive the baseline from the workdir and
compare text, style/structure, protected XML, revisions/comments, and package
parts. They do not trust `build`'s intermediate result.

## Freshness gate

The store/edit state is authoritative. A workdir must be clean before build or
verification. Dirty, stale, conflicting, malformed, or drifted state fails
closed; there is no bypass flag.

| State | Meaning | Allowed to build? |
|---|---|---|
| `clean` | canonical and draft state agree | yes |
| `dirty` | a draft exists but is not committed | no; commit through edit/MCP |
| `stale-clean` | source changed without refresh | no; inspect/reconcile |
| `conflict` | competing changes require resolution | no; stop and report |

## Build and verify gate

`build` atomically publishes a new DOCX only after checking:

- typed grammar and paragraph/leaf structure;
- style, token, anchor, and protected-node invariants;
- source/template fingerprints and manifest lineage;
- store generation and writer-lane identity;
- clean state and operation-id replay safety.

`verify` independently checks the published output. A successful build without
successful verify is not a delivery.

## Byte-fidelity gate

- A no-op extract/build retains the source package bytes.
- Untouched package parts remain byte-identical.
- Only requested text islands or explicit structural parts may change.
- Revision/comment/table decisions that create a new baseline never mutate the
  source workdir.
- Failed guards leave workdir and output bytes unchanged.

## MCP gate

The 36-tool MCP surface is frozen by `.mcp_schemas.json`. The live Rust
`tools/list` response must match every schema field, required list, type,
default, and property exactly. The repository gate is:

```powershell
cargo test --test review60 mcp_tool_surface_is_frozen_36_with_exact_published_schemas
cargo test --test review60 mcp_tools_accept_schema_derived_minimal_arguments
powershell -File qualification/rust_mcp_schema_gate.ps1
```

The MCP process emits only `OK <json>` / `ERR <message>` on stdout. Logs belong
on stderr. Production configuration uses the installed Rust binary's absolute
path and `args: ["mcp"]`.

## Office boundary

When a real Word or LibreOffice host is available, perform a manual save/reopen
check on the final DOCX and record the host/version. On Windows, pass native
paths to LibreOffice, not MSYS `/d/...` paths:

```powershell
& 'C:\Program Files\LibreOffice\program\soffice.exe' `
  --headless --convert-to pdf --outdir <dir> <output.docx>
```

Page reflow from text-length changes is expected. Repair warnings or structural
damage are failures. When no host is available, record
`office.status=not-run-no-host` and keep release readiness false. This project
does not build Office COM automation and does not turn a missing host into a
pass.

## Repository gates

For Rust code changes:

```powershell
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build --release
powershell -File qualification/rust_mcp_schema_gate.ps1
```

The real-document and packaging qualification gates remain explicit:

```powershell
powershell -File qualification/rust_tracer62_gate.ps1
powershell -File scripts/rust61_packaging_tests.ps1
```

The Python reference may be installed and invoked only as an offline oracle for
differential tests. It is not evidence that the production Rust resolver,
installer, or MCP configuration works.

## Claim-to-gate map

| Claim | Evidence |
|---|---|
| Workdir is readable and safe | `inspect` / `workdir_open` |
| Text edit is committed | `edit text` result or MCP `commit_sync` |
| Output was built | `build` / `build_docx` result |
| Output is correct | independent `verify` / `verify_output` |
| MCP contract is frozen | exact schema test + `rust_mcp_schema_gate.ps1` |
| Installed runtime is Rust-only | installer receipt, absolute MCP path, release smoke |
| Office-compatible | actual host save/reopen; otherwise `not-run-no-host` |
