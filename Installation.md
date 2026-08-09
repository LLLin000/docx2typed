# docx2typed installation for agents

Use this file when a user asks you to install or configure `docx2typed`.
The published package name is **`docx2typed`**.

## Skill installation

Skill installation is an agent-side setup step. When the user explicitly asks
for `docx2typed` installation, the agent should use the host's normal skill
manager and installation location before configuring the package or MCP.
Do not ask the user to copy `SKILL.md` or guess a platform-specific skills
directory. Verify that the skill is loaded, then continue with the package
and host configuration steps below.

## 1. Inspect before changing the environment

Check the runtime and whether the CLI already resolves:

```bash
python --version
python -m pip show docx2typed
command -v docx2typed  # Windows PowerShell: Get-Command docx2typed
```

Use Python 3.11 or newer. Keep an existing working installation unless the user
asks for an upgrade.

## 2. Install from PyPI

For a normal Python environment:

```bash
python -m pip install --upgrade docx2typed
```

For an isolated CLI installation with `uv`:

```bash
uv tool install --upgrade docx2typed
```

For a one-shot invocation without a persistent install:

```bash
uvx docx2typed extract input.docx -o workdir
```

Prefer `uv tool install` for a human's persistent CLI and `uvx` for an MCP
host or another agent-managed process. Do not install into the system Python
when a project virtual environment is available.

## 3. Verify the installation

Run the smallest checks that cover the requested path:

```bash
docx2typed extract --help
docx2typed review --help
```

The package must expose these commands:

- `docx2typed` — typed-mode CLI, including `mcp` and `review` subcommands.
- `docx2typed-mcp` — stdio MCP server.
- `docx2typed-review` — local review server.

If the command is not found after installation, use the absolute executable
from the active virtual environment and report the environment mismatch. Do
not silently fall back to an uninstalled source checkout.

## 4. Configure MCP only with user authorization

The shortest Claude Code configuration is:

```bash
claude mcp add docx2typed -- uvx docx2typed mcp
```

For an MCP host that consumes JSON configuration:

```json
{
  "mcpServers": {
    "docx2typed": {
      "command": "uvx",
      "args": ["docx2typed", "mcp"]
    }
  }
}
```

For a host that cannot resolve `uvx`, use the absolute executable from the
same environment where `docx2typed` was installed:

```json
{
  "mcpServers": {
    "docx2typed": {
      "command": "python",
      "args": ["-m", "docx2typed", "mcp"]
    }
  }
}
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

1. `docx2typed` resolves from the intended environment.
2. The requested CLI help command succeeds.
3. The MCP entry uses the installed package, not a repository-relative import.
4. Tailscale mode, when requested, prints a tailnet URL and does not fall back
   to `0.0.0.0`.
5. Existing user configuration remains intact except for explicitly authorized
   additions.

For source development, use `python -m pip install -e .` from the repository
instead of mixing a source import with the PyPI installation.
