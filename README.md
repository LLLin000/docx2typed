# docx2typed

[中文版本](README.zh-CN.md) · [Installation and collaboration](Installation.md)

> Structure-preserving DOCX editing with a signed Rust binary, MCP review workflows, and independent verification.

`docx2typed` edits `.docx` text without flattening the document into lossy plain text or HTML. Existing formatting, tracked revisions, comments, tables, content controls, anchors, and untouched package parts stay protected by the typed workdir and byte-preserving build pipeline.

## Runtime boundary

Production uses the self-contained **Rust binary** only:

- CLI: `docx2typed <command>`
- MCP: `docx2typed mcp` over clean stdio
- Review server: `docx2typed review <workdir>`
- Python: offline reference/oracle for development qualification only; never a production fallback

The current release candidate is qualified for Rust CLI/MCP execution and real-document migration. Office save/reopen qualification remains an honest `not-run-no-host` release gate when no Word/LibreOffice host is available; this repository does not build Office COM automation.

## Quick start from a checkout

Build the release binary:

```powershell
cargo build --release
```

On Windows, install it with the receipt-safe lifecycle installer:

```powershell
powershell -File scripts/install_binary.ps1 `
  -Action install `
  -Bin target\release\docx2typed.exe
```

Verify the installed binary:

```powershell
docx2typed --version --json
docx2typed extract input.docx -o workdir --json
```

The installer writes `%LOCALAPPDATA%\docx2typed\receipt.json` and
`mcp.config.json`. Use `-Action update`, `rollback`, or `uninstall` for the
same receipt-owned installation.

## Edit a document

The source DOCX is never overwritten. A workdir contains the extracted typed
state, immutable template, fingerprints, and generation store.

```powershell
# Discover editable leaf paths and document structure
docx2typed enumerate workdir --json

# Edit one text leaf; P0.0 is an example leaf path
docx2typed edit text workdir P0.0 "old text" "new text" --json

# Build and independently verify a new DOCX
docx2typed build workdir -o output.docx --json
docx2typed verify workdir output.docx --json
```

Use the MCP server for agent-driven edits. Configure the host with the
absolute installed path and the single `mcp` argument:

```json
{
  "mcpServers": {
    "docx2typed": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\docx2typed\\bin\\docx2typed.exe",
      "args": ["mcp"]
    }
  }
}
```

The MCP surface has 36 frozen tools. A normal session is:

```text
workdir_open → list_paragraphs/get_paragraph → replace_text or batch_edit
→ diff_preview → commit_sync → build_docx → verify_output
```

The review tools add revision decisions, comment handling, table structure
operations, and human-agent queue handoff without bypassing the store or
independent verifier.

## Browser review

Start the local review server after extracting a workdir:

```powershell
docx2typed review workdir --host 127.0.0.1 --port 8876
```

Open <http://127.0.0.1:8876/>. The browser is a review and handoff surface;
it does not silently rewrite the source DOCX. Human decisions are queued or
exported, then the agent applies them transactionally and rebuilds the output.

For a phone or another machine, bind only to an explicitly controlled private
interface and apply the host's network controls. The Rust binary has no
`--tailscale` mode and does not silently broaden a local bind to `0.0.0.0`.

## Revision, comment, and table operations

```powershell
# Inspect tracked revisions in a DOCX or workdir
docx2typed revisions list workdir --json

# Inspect one revision view
docx2typed revisions view workdir accept --json

# Apply one decision with a fingerprint guard
docx2typed decide accept "part|kind|w:id|fingerprint" `
  --workdir workdir --fingerprint <fingerprint> --json

# List or explicitly delete comments
docx2typed comment list workdir --json
docx2typed comment delete workdir <comment-id> --json

# Table operations create a new DOCX and a new clean workdir
docx2typed decide table-insert-row T0 --workdir workdir `
  --args "1" --output table.docx --workdir-out table-workdir --json
```

Comments remain by default. Delete one only when the user explicitly asks.
Table operations never rewrite cell text; merge refuses content loss unless
`--discard-content` is explicit.

## Delivery gate

Every delivery follows:

```text
extract → inspect/edit → build → independent verify → optional Office check
```

`verify` is independent of `build`: it re-derives the baseline and checks text,
styles, protected structures, revisions/comments, and package-part identity.
A clean build is not a substitute for verification.

## Further reading

- [Installation and collaboration guide](Installation.md)
- [CLI and MCP capabilities](capabilities.md)
- [End-to-end workflows](composites.md)
- [Verification guarantees](verification.md)
- [Agent skill](SKILL.md)
