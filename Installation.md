# docx2typed installation for agents

Use this file when a user asks you to install or configure `docx2typed`.
Production resolves **only the signed Rust binary** installed by
`scripts/install_binary.ps1`; there is no Python runtime fallback. The Python
reference remains only an offline qualification oracle / diagnostic rollback
asset, never a production runtime.

## Skill installation

Skill installation is an agent-side setup step. When the user explicitly asks
for `docx2typed` installation, the agent should use the host's normal skill
manager and installation location before configuring the package or MCP.
Do not ask the user to copy `SKILL.md` or guess a platform-specific skills
directory. Verify that the skill is loaded, then continue with the binary
and host configuration steps below.

## 1. Inspect before changing the environment

Check whether the CLI already resolves and which binary it is:

```bash
Get-Command docx2typed  # Windows PowerShell
docx2typed --version --json
```

The engine must report `"name": "docx2typed-rust"` (the signed Rust binary).
Keep an existing working installation unless the user asks for an upgrade.

## 2. Install the signed Rust binary

For a Windows host, install the self-contained signed release binary with the
receipt-safe lifecycle installer:

```powershell
powershell -File scripts/install_binary.ps1 -Action install -Bin target\release\docx2typed.exe
```

The installer atomically publishes, under `%LOCALAPPDATA%\docx2typed` (or an
explicit `-Prefix`):

- `bin\docx2typed.exe` — the installed signed binary (no Python, no venv);
- `receipt.json` — install receipt (version, absolute binary path, SHA-256,
  install date, previous-version bookkeeping);
- `mcp.config.json` — MCP config snippet whose `command` is the absolute
  path of the installed binary with `args: ["mcp"]`.

The lifecycle supports `install` / `update` / `rollback` / `uninstall`, each
atomic and receipt-safe. `update` keeps the previous binary as
`bin\docx2typed.exe.bak`; `rollback` restores it atomically; `uninstall`
removes only receipt-listed files whose hashes still match.

The Python package in this repository is the frozen offline Reference
(oracle). It is installed only for differential qualification (for example
`python -m pip install -e .` in a development checkout) and never resolves
production CLI, MCP, or Skill configuration.

## 3. Verify the installation

Run the smallest checks that cover the requested path:

```bash
docx2typed --version --json
docx2typed extract --help
docx2typed review --help
```

The signed binary must expose these commands:

- `docx2typed <command>` — Protocol-major-1 CLI: `extract`, `inspect`,
  `migrate`, `validate`, `view`, `edit`, `build`, `verify`, `decide`,
  `audit`, `revisions`, `comment`, `store-state`, `enumerate`.
- `docx2typed mcp` — stdio MCP server (36 tools).
- `docx2typed review WORKDIR` — local single-session review server.

If the command is not found after installation, report the environment
mismatch. Do not silently fall back to a Python installation or an
uninstalled source checkout.

## 4. Configure MCP only with user authorization

The installer already writes `mcp.config.json` with the absolute binary path:

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

Use that exact absolute path — never `python`, `uvx`, or a relative
executable name. For Claude Code:

```bash
claude mcp add docx2typed -- "C:\Users\<you>\AppData\Local\docx2typed\bin\docx2typed.exe" mcp
```

Do not edit a user's MCP configuration, install a skill, or restart a host
without explicit authorization. If authorization is given, preserve existing
servers and add only the `docx2typed` entry.

## 5. Configure temporary phone collaboration over Tailscale

Use the machine that owns the typed workdir as the server:

```bash
docx2typed review WORKDIR --tailscale --port 8876
```

The server queries `tailscale ip -4`, binds only to that Tailscale IPv4
address, and prints the URL to open on a phone. The phone and server must be
signed in to the same tailnet. The review page polls the shared server state,
so the phone can inspect revisions and submit review decisions while the
agent continues using MCP.

This is a temporary private-network setup:

- restrict collaborators with Tailscale ACLs;
- keep the server bound with `--tailscale`;
- do not expose the review port with `--host 0.0.0.0`;
- treat the HTTP URL as tailnet-only and do not publish it publicly.

If `tailscale` is missing or has no IPv4 address, report the exact diagnostic
and finish the local installation; do not silently bind to another interface.

## 6. Agent handoff completion criteria

Installation is complete only when all applicable checks pass:

1. `docx2typed` resolves from the intended environment and reports
   `docx2typed-rust`.
2. The requested CLI help command succeeds.
3. The MCP entry uses the installed signed binary's absolute path, not a
   Python launcher or a repository-relative import.
4. Tailscale mode, when requested, prints a tailnet URL and does not fall back
   to `0.0.0.0`.
5. Existing user configuration remains intact except for explicitly authorized
   additions.

For source development of the Rust tracer, build with `cargo build --release`
and install the artifact with `scripts/install_binary.ps1`; never mix a
Python source import with the production installation.
