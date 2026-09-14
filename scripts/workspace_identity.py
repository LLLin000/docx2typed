"""Workspace identity: the permanent Family and Workspace ids (design 2026-09-14).

The identity model is deliberately not "DOCX → workspace":

    Family F1 ──┬── source.docx
                ├── 老师二审.docx          (managed exports)
                ├── Word-edited 版本        (observations)
                └── 邮件回来的拷贝
        │
        └── Workspace WA (this machine's managed workdir) ── V1, V2, V3 …

``family_id`` is the logical document's lineage and never changes; a DOCX is
only an *observation* that a resolver maps back to a family. ``workspace_id``
names one managed workdir for that family, so a second machine can hold the
same family with a different workspace.

The ids live in ``workspace.json`` at the workdir root — engine-owned, written
by the engine, and *not* part of the store pointer (``workdir.json`` is rebuilt
by the store lane on every commit and would drop foreign keys). Losing the
global resolver registry must never damage this file or the version history:
the registry is a cache, this file is authoritative.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .typed_docx import ValidationError

IDENTITY_FILE = "workspace.json"
IDENTITY_SCHEMA = "docx2typed-workspace-1"


def new_family_id() -> str:
    return "f_" + uuid.uuid4().hex


def new_workspace_id() -> str:
    return "ws_" + uuid.uuid4().hex


def read_identity(workdir: str | Path) -> dict[str, Any] | None:
    """The workdir's identity, or None when it has none yet (legacy workdir)."""
    path = Path(workdir) / IDENTITY_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != IDENTITY_SCHEMA:
        return None
    if not data.get("family_id") or not data.get("workspace_id"):
        return None
    return data


def write_identity(workdir: str | Path, identity: dict[str, Any]) -> Path:
    """Atomically publish one identity file (temp + replace, LF newlines)."""
    target = Path(workdir) / IDENTITY_FILE
    payload = dict(identity)
    payload["schema"] = IDENTITY_SCHEMA
    fd, temp_name = tempfile.mkstemp(prefix=f".{IDENTITY_FILE}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target


def ensure_identity(
    workdir: str | Path,
    *,
    origin: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """(identity, created). Mints ids for a workdir that has none — the one
    idempotent write an old workspace receives, and the reason its history stays
    addressable without a migration."""
    existing = read_identity(workdir)
    if existing is not None:
        return existing, False
    root = Path(workdir)
    if not (root / "typed.md").is_file():
        raise ValidationError(f"not a typed workdir: {root}")
    identity = {
        "family_id": new_family_id(),
        "workspace_id": new_workspace_id(),
        "created_at": _now(),
        "origin_family": (origin or {}).get("family_id"),
        "origin_version": (origin or {}).get("version"),
    }
    write_identity(root, identity)
    return read_identity(root) or identity, True


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
