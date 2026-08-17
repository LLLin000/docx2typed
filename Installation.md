# docx2typed installation and agent setup

This guide installs the **Rust production binary** and connects an agent to its
MCP server. It intentionally separates three things that are often confused:

1. obtaining a target-matched binary;
2. installing that binary on the host;
3. adding one authorized MCP entry.

The Python implementation is maintained in
[`LLLin000/docx2typed`](https://github.com/LLLin000/docx2typed). It is an offline
qualification reference, not a Rust runtime dependency.

## Choose one binary source

### A. Build from a checkout

Use this for development or when no release bundle is available:

```text
cargo build --release --locked
```

The output is target-specific:

```text
Windows: target\release\docx2typed.exe
Unix:    target/release/docx2typed
```

### B. Use a release bundle

A target-matched bundle contains the self-contained binary and its release
metadata:

```text
docx2typed-<target>-stable/
├── docx2typed[.exe]
├── SHA256SUMS.txt
├── SHA256SUMS.txt.sig
├── provenance.json
├── reproducibility.txt
├── sbom.json
├── licenses/
└── ...
```

Verify `SHA256SUMS.txt` and its detached signature before copying the binary.
The bundle does not contain Python, a background service, or the repository's
PowerShell installer. The current repository qualifies bundles through GitHub
Actions; it does not currently publish a universal package-manager installer.

### C. Release operator workflow

From a Windows checkout, the release operator can produce a signed bundle:

```powershell
cargo build --release --locked
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\package_release.ps1 `
  -Bin .\target\release\docx2typed.exe `
  -Target windows-x86_64-msvc `
  -Channel stable `
  -Coverage this-host
```

Packaging requires the signing-key policy described by
[`reference/keys/README.md`](reference/keys/README.md). A dev-key bundle is a
reproducibility artifact, not a public release.

## Windows installation

The receipt-safe installer is Windows-only. It accepts an already-built or
already-verified binary; it does not download one.

```powershell
$source = (Resolve-Path .\target\release\docx2typed.exe).Path
& $source --version --json

powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_binary.ps1 `
  -Action install `
  -Bin $source
```

Default layout:

```text
%LOCALAPPDATA%\docx2typed\
├── bin\docx2typed.exe       installed Rust binary
├── receipt.json              version, SHA-256, and owned paths
└── mcp.config.json           MCP snippet with the absolute binary path
```

The installer does not modify `PATH`. Use the absolute path in automation, or
opt into the current PowerShell session:

```powershell
$bin = Join-Path $env:LOCALAPPDATA 'docx2typed\bin\docx2typed.exe'
$env:Path = "$(Split-Path $bin);$env:Path"
& $bin --version --json
```

If using a release bundle instead of a checkout, either run the installer from
a matching repository checkout or copy the verified binary to a user-owned
location and use that absolute path in MCP. Do not pretend that a copied binary
has a receipt unless the installer created one.

Lifecycle operations require an existing receipt:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_binary.ps1 `
  -Action update `
  -Bin .\target\release\docx2typed.exe

powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_binary.ps1 -Action rollback

powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_binary.ps1 -Action uninstall
```

`update` keeps one verified backup. `rollback` consumes that backup.
`uninstall` removes only receipt-owned files whose recorded hashes still match;
it refuses to guess after user changes.

## macOS/Linux installation

There is no cross-platform installer in this repository. Copy a verified,
target-matched binary to a user-owned directory:

```bash
mkdir -p "$HOME/.local/bin"
cp target/release/docx2typed "$HOME/.local/bin/docx2typed"
chmod 755 "$HOME/.local/bin/docx2typed"
export PATH="$HOME/.local/bin:$PATH"
docx2typed --version --json
```

For a persistent `PATH`, add `$HOME/.local/bin` through the host's normal shell
configuration. The binary remains self-contained; the host does not need the
Rust toolchain after installation.

## Configure the agent MCP entry

Use the installed binary's **absolute path**. This is the canonical shape:

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

On macOS/Linux, replace `command` with the absolute path to the copied binary.
On Windows, the installer writes this object to
`%LOCALAPPDATA%\docx2typed\mcp.config.json`.

Copy the object into the host's configuration only after authorization. Keep
all existing MCP servers. Do not replace the command with `python`, `uvx`,
`cargo run`, a relative path, or a repository import.

## Verify the installation

Use the installed absolute path when `PATH` is not configured:

```powershell
$bin = Join-Path $env:LOCALAPPDATA 'docx2typed\bin\docx2typed.exe'
& $bin --version --json
& $bin extract input.docx -o workdir --json
& $bin enumerate workdir --json
& $bin build workdir -o output.docx --json
& $bin verify workdir output.docx --json
```

On Unix, replace `$bin` with the absolute path to `docx2typed` and use the same
subcommands. The descriptor must report `"name": "docx2typed-rust"`.

The production command surface is:

```text
extract, build, verify, inspect, migrate, edit, store-state,
enumerate, revisions, decide, comment, audit, mcp, review
```

The binary has no separate `--help` command; use
[`capabilities.md`](capabilities.md) for the exact arguments.

## MCP stdio smoke

The MCP process reads one JSON request per line and writes one protocol reply
per line. Logs belong on stderr:

```text
{"tool":"engine_info","args":{}}
{"tool":"tools/list","args":{}}
```

Expected result: two `OK` lines and 36 frozen tools in `tools/list`.

## Browser review

After extracting a workdir, start the local review server with the same binary:

```powershell
& $bin review workdir --host 127.0.0.1 --port 8876
```

Open `http://127.0.0.1:8876/`. The browser queues decisions and patches; it does
not write the DOCX behind the agent's back. The agent applies the queue,
builds a new output, and independently verifies it.

The Rust binary has no `--tailscale` option and does not silently broaden a local
bind to `0.0.0.0`. Remote access requires an explicitly controlled private
interface and host ACLs.

## Skill setup and completion criteria

Use the host's normal skill manager to enable this repository's `SKILL.md`. Do
not create a second copy unless the host manager requires it, and preserve the
user's existing skills and MCP entries.

Installation is complete only when:

1. the selected Rust binary reports `docx2typed-rust`;
2. `extract` and `enumerate` succeed on a representative DOCX;
3. `build` and independent `verify` succeed for the requested path;
4. the MCP entry uses the installed absolute Rust binary and `args: ["mcp"]`;
5. the review server, when requested, binds only to the authorized interface;
6. existing user configuration is unchanged except for explicitly authorized
   additions.

Office save/reopen is a separate host-dependent qualification gate. If no real
Word/LibreOffice host is available, report `not-run-no-host`; do not invent a
pass and do not build Office COM automation.
