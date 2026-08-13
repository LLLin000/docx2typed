"""Thin capture-only adapters over the public CLI and MCP seams.

Adapters observe raw inputs and outputs; they never interpret, compare, or
decide.  Every capture is a passive record: a return code, raw byte streams,
or file hashes.  Comparison policy lives exclusively in scripts/qualify.py.

The MCP session uses the same stdio driver loop as release_acceptance; the
transport ``OK``/``ERR`` prefix is transport status, not a product judgment.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

_WORD_PARTS = re.compile(rb"word/.*\.xml$")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Capture:
    """One raw observation: return code plus unmodified byte streams."""

    rc: int
    stdout: bytes
    stderr: bytes
    duration_ms: int


def capture_cli(command: list[str], cwd: Path = REPO_ROOT, timeout: int = 60) -> Capture:
    """Run an external command and record its raw output.  No interpretation.

    ``timeout`` is a hard per-command budget: a command that cannot finish in
    time raises subprocess.TimeoutExpired (a raw observation that the command
    did not complete); the runner diagnoses that as not-run, never pass.
    """
    started = time.monotonic()
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
    )
    return Capture(
        rc=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


_MCP_DRIVER_LOOP = r"""
import json, sys, importlib
server = importlib.import_module("scripts.mcp_server")
tm = server.mcp._tool_manager
for line in sys.stdin:
    request = json.loads(line)
    try:
        out = tm.get_tool(request["tool"]).fn(**request["args"])
        if hasattr(out, "model_dump"):
            out = out.model_dump()
        print("OK " + json.dumps(out, ensure_ascii=True), flush=True)
    except Exception as exc:  # noqa: BLE001 - transport surfaces any tool error
        print("ERR " + str(exc), flush=True)
"""


class McpSession:
    """One persistent stdio session against the MCP server module.

    A fresh session per check/scenario so session state never leaks between
    checks; the caller closes it.  Every tool call is bounded by a hard
    deadline: a driver that stalls is killed and the call raises TimeoutError,
    which the runner diagnoses as not-run, never pass.
    """

    CALL_TIMEOUT_SECONDS = 60

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _MCP_DRIVER_LOOP],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None

    def call(self, tool: str, **args: Any) -> Capture:
        """Send one request line and capture the raw reply line verbatim."""
        started = time.monotonic()
        request = json.dumps({"tool": tool, "args": args})
        self.proc.stdin.write(request.encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        timer = threading.Timer(self.CALL_TIMEOUT_SECONDS, self.proc.kill)
        timer.start()
        try:
            line = self.proc.stdout.readline()
        finally:
            timer.cancel()
        if not line and self.proc.poll() is not None:
            raise TimeoutError(f"mcp tool {tool} timed out after {self.CALL_TIMEOUT_SECONDS}s")
        return Capture(rc=0, stdout=line, stderr=b"", duration_ms=int((time.monotonic() - started) * 1000))

    def close(self) -> None:
        """End the driver: EOF on stdin first (the loop exits on empty
        input), then terminate and reap the process so no child lingers
        between executions."""
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


def capture_docx_parts(path: Path) -> dict[str, str] | None:
    """Per-part SHA-256 map of a .docx; None when it is not a readable zip."""
    try:
        with zipfile.ZipFile(path) as archive:
            return {name: sha256_hex(archive.read(name)) for name in archive.namelist()}
    except Exception:  # noqa: BLE001 - unreadable file is a raw observation
        return None


def capture_zip_members(path: Path) -> dict[str, bytes] | None:
    """Raw member bytes of a .docx (word parts only); None when unreadable."""
    try:
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name)
                for name in archive.namelist()
                if _WORD_PARTS.match(name.encode("utf-8"))
            }
    except Exception:  # noqa: BLE001
        return None


def soffice_path() -> str | None:
    """LibreOffice binary: Windows path, else PATH lookup.  Discovery only."""
    windows = Path(r"C:/Program Files/LibreOffice/program/soffice.exe")
    if windows.exists():
        return str(windows)
    import shutil

    return shutil.which("soffice")
