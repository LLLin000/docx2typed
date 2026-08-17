# docx2typed installation for agents

This guide installs the Rust production runtime and connects an agent to its
MCP server. The Python package in this repository is an offline reference/oracle
for qualification only; it is not a production CLI, MCP server, or fallback.

## 1. Skill setup

Use the host's normal skill manager to enable this repository's `SKILL.md`.
The agent owns the host-specific skill location. Do not create a second copy of
the skill unless the host manager requires it, and preserve the user's existing
skills and MCP entries.

## 2. Build or obtain the Rust binary

From a source checkout:

```powershell
cargo build --release
```

The Windows release artifact is:

```text
target\release\docx2typed.exe
```

Verify the artifact before installing it:

```powershell
& .\target\release\docx2typed.exe --version --json
```

The descriptor must report `"name": "docx2typed-rust"`.

## 3. Install on Windows

Use the receipt-safe lifecycle installer:

```powershell
powershell -File scripts/install_binary.ps1 `
  -Action install `
  -Bin target\release\docx2typed.exe
```

Default layout:

```text
%LOCALAPPDATA%\docx2typed\
├── bin\docx2typed.exe       installed Rust binary
├── receipt.json              version, absolute path, SHA-256, ownership
└── mcp.config.json           MCP snippet with the absolute binary path
```

The installer does not modify `PATH`. Either call the absolute path or add
`%LOCALAPPDATA%\docx2typed\bin` to the user's PATH through the normal system
settings. In the current PowerShell session:

```powershell
$env:Path += ";$env:LOCALAPPDATA\docx2typed\bin"
docx2typed --version --json
```

Lifecycle operations:

```powershell
powershell -File scripts/install_binary.ps1 -Action update -Bin target\release\docx2typed.exe
powershell -File scripts/install_binary.ps1 -Action rollback
powershell -File scripts/install_binary.ps1 -Action uninstall
```

`update` keeps the previous binary as `bin\docx2typed.exe.bak`. `rollback`
restores it atomically. `uninstall` removes only receipt-owned files whose
recorded hashes still match; it refuses to guess when user state changed.

## 4. Verify the installed runtime

Use the installed absolute path when PATH is not configured:

```powershell
$bin = Join-Path $env:LOCALAPPDATA 'docx2typed\bin\docx2typed.exe'
& $bin --version --json
& $bin extract input.docx -o workdir --json
& $bin enumerate workdir --json
& $bin build workdir -o output.docx --json
& $bin verify workdir output.docx --json
```

The Rust CLI has a finite command set. The supported production commands are:

```text
extract, build, verify, inspect, migrate, edit, store-state,
enumerate, revisions, decide, comment, audit, mcp, review
```

There is no Python launcher, `python -m docx2typed` production path, or silent
fallback to a source checkout. The binary is hand-rolled and does not expose a
separate `--help` command; use this guide and `capabilities.md` for syntax.

## 5. Configure MCP

The installer writes `mcp.config.json`. Its effective shape is:

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

Use that exact absolute path. Do not replace it with `python`, `uvx`, a
relative path, or a repository import. If the host has a CLI MCP manager:

```powershell
claude mcp add docx2typed -- `
  "$env:LOCALAPPDATA\docx2typed\bin\docx2typed.exe" mcp
```

Only add or change the host entry after authorization. Preserve every existing
MCP server. A minimal stdio smoke is:

```powershell
'{"tool":"engine_info","args":{}}' + "`n" +
'{"tool":"tools/list","args":{}}' |
  & "$env:LOCALAPPDATA\docx2typed\bin\docx2typed.exe" mcp
```

Expected results are `OK` lines only, with 36 frozen tools in `tools/list`.

## 6. Start browser review

After extracting a workdir:

```powershell
docx2typed review workdir --host 127.0.0.1 --port 8876
```

Open `http://127.0.0.1:8876/`. The review server is a local single-session
surface. The browser queues decisions and patches; it does not write the DOCX
behind the agent's back. The agent applies the queue through the MCP review
lane, rebuilds, and independently verifies the output.

The Rust binary has no `--tailscale` option. For remote private-network use,
bind only to an explicitly controlled interface and apply the network's ACLs;
do not publish the review port to the public Internet.

## Completion criteria

Installation is complete only when all applicable checks pass:

1. The intended Rust binary reports `docx2typed-rust`.
2. `extract` and `enumerate` succeed on a representative DOCX/workdir.
3. `build` and independent `verify` succeed for the requested path.
4. The MCP entry uses the installed absolute Rust binary and `args: ["mcp"]`.
5. The review server, when requested, binds only to the authorized interface.
6. Existing user skill and MCP configuration remains unchanged except for
   explicitly authorized additions.

Office save/reopen is a separate host-dependent qualification gate. When no
Word/LibreOffice host is available, report `not-run-no-host`; do not invent a
pass and do not build Office COM automation.
