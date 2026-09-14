"""Resolver registry: deterministic ``DOCX observation → family`` (cache, not truth).

The authoritative identity lives in each workspace's ``workspace.json``; this
registry only answers "which family does this file instance resolve to?", and
deleting it must never damage a workspace or its version history — the worst
case is one re-location or one adoption question.

Evidence grades (design 2026-09-14), strongest first:

    P0  explicit family/workspace from the caller
    P1  exact sha256 known for exactly one family            -> auto
    P2  ADS/xattr carries a family id                        -> auto (opt-in carrier)
    P3  local file object known AND its sha256 known too     -> auto (proof)
    C1  local file object known, content hash unknown        -> ask once (adopt)
    C2  docId / WPS hdid / created hints match               -> candidate only
    U   nothing                                              -> new workspace or fork

"Same bytes" is a fact about the document; "same file object" is only a fact
about this machine's filesystem — which is why a changed hash never adopts
itself: a file can be truncated and rewritten with another document's bytes
while keeping its file id.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from .workspace_identity import read_identity

REGISTRY_SCHEMA = "docx2typed-resolver-1"
DB_NAME = "registry.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    family_id    TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    path         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (family_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256       TEXT NOT NULL,
    family_id    TEXT NOT NULL,
    workspace_id TEXT,
    version      TEXT,
    kind         TEXT NOT NULL,
    path         TEXT,
    volume       TEXT,
    file_id      TEXT,
    doc_id       TEXT,
    wps_hdid     TEXT,
    created_at   TEXT,
    observed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_sha ON observations(sha256);
CREATE INDEX IF NOT EXISTS idx_observations_file ON observations(volume, file_id);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    event     TEXT NOT NULL,
    family_id TEXT,
    workspace_id TEXT,
    detail    TEXT
);
"""


def registry_path(root: str | Path | None = None) -> Path:
    """The resolver cache. ``$DOCX2TYPED_REGISTRY`` overrides the default so a
    test or a relocation can point it somewhere else; the cache is disposable."""
    if root is not None:
        return Path(root)
    override = os.environ.get("DOCX2TYPED_REGISTRY")
    if override:
        return Path(override)
    return Path.home() / ".docx2typed" / DB_NAME


@contextlib.contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """One short-lived connection per call: the cache is a file a user may
    delete or move, and a held handle would block that on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(_SCHEMA)
        yield connection
        connection.commit()
    finally:
        connection.close()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# File facts the registry keys on
# --------------------------------------------------------------------------

def file_identity(path: Path) -> tuple[str, str]:
    """(volume, file_id) for one file — stable across rename and in-place
    saves on the same volume, and *only* that (a copy is a new object)."""
    try:
        stat = path.stat()
    except OSError:
        return "", ""
    return f"{stat.st_dev:#x}", f"{stat.st_ino:#x}"


def content_hash(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lineage_hints(path: Path) -> dict[str, str]:
    """docId / WPS hdid / created — *hints only*: measured to collide between
    documents that were saved from the same source, so they may nominate a
    candidate and never resolve one."""
    hints = {"doc_id": "", "wps_hdid": "", "created": ""}
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            settings = archive.read("word/settings.xml").decode("utf-8") if "word/settings.xml" in names else ""
            core = archive.read("docProps/core.xml").decode("utf-8") if "docProps/core.xml" in names else ""
            custom = archive.read("docProps/custom.xml").decode("utf-8") if "docProps/custom.xml" in names else ""
    except (OSError, zipfile.BadZipFile, KeyError):
        return hints
    match = re.search(r'<w15:docId[^>]*w15:val="([^"]+)"', settings)
    if match:
        hints["doc_id"] = match.group(1)
    match = re.search(r"<dcterms:created[^>]*>([^<]*)<", core)
    if match:
        hints["created"] = match.group(1)
    match = re.search(
        r'<property[^>]*name="KSOTemplateDocerSaveRecord"[^>]*>(.*?)</property>', custom, re.S
    )
    if match:
        payload = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        try:
            decoded = base64.b64decode(payload).decode("utf-8", errors="replace")
            hdid = json.loads(decoded).get("hdid")
            if isinstance(hdid, str):
                hints["wps_hdid"] = hdid
        except (ValueError, json.JSONDecodeError):
            pass
    return hints


# --------------------------------------------------------------------------
# Registry operations (best-effort: a broken cache never blocks work)
# --------------------------------------------------------------------------

def record_workspace(root: Path, identity: dict[str, Any], *, path: str | None = None) -> None:
    try:
        with _connect(root) as db:
            db.execute(
                "INSERT INTO workspaces (family_id, workspace_id, path, created_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(family_id, workspace_id) DO UPDATE SET path=excluded.path, last_seen_at=excluded.last_seen_at",
                (
                    identity["family_id"],
                    identity["workspace_id"],
                    str(path if path is not None else identity.get("path", "")),
                    identity.get("created_at") or _now(),
                    _now(),
                ),
            )
    except sqlite3.Error:
        pass


def observe(
    root: Path,
    *,
    sha256: str,
    family_id: str,
    workspace_id: str | None = None,
    version: str | None = None,
    kind: str,
    path: str | Path | None = None,
    hints: dict[str, str] | None = None,
) -> None:
    """Record one file instance the engine has seen for a family."""
    file_path = Path(path) if path is not None else None
    volume, file_id = file_identity(file_path) if file_path is not None else ("", "")
    hints = hints or {}
    try:
        with _connect(root) as db:
            db.execute(
                "INSERT INTO observations (sha256, family_id, workspace_id, version, kind, path,"
                " volume, file_id, doc_id, wps_hdid, created_at, observed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sha256, family_id, workspace_id, version, kind,
                    str(file_path) if file_path is not None else None,
                    volume, file_id, hints.get("doc_id", ""), hints.get("wps_hdid", ""),
                    hints.get("created", ""), _now(),
                ),
            )
    except sqlite3.Error:
        pass


def record_event(root: Path, event: str, *, family_id: str | None = None,
                 workspace_id: str | None = None, detail: Any = None) -> None:
    try:
        with _connect(root) as db:
            db.execute(
                "INSERT INTO events (at, event, family_id, workspace_id, detail) VALUES (?, ?, ?, ?, ?)",
                (_now(), event, family_id, workspace_id, json.dumps(detail, ensure_ascii=False) if detail is not None else None),
            )
    except sqlite3.Error:
        pass


def families_for_sha256(root: Path, sha256: str) -> list[dict[str, Any]]:
    """Every family this exact content has been seen for — one hash can belong
    to several families (two projects started from the same template)."""
    try:
        with _connect(root) as db:
            rows = db.execute(
                "SELECT family_id, MAX(observed_at) AS seen FROM observations WHERE sha256 = ?"
                " GROUP BY family_id ORDER BY seen DESC",
                (sha256,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def workspace_for(root: Path, family_id: str, workspace_id: str | None = None) -> dict[str, Any] | None:
    try:
        with _connect(root) as db:
            if workspace_id:
                row = db.execute(
                    "SELECT * FROM workspaces WHERE family_id = ? AND workspace_id = ?",
                    (family_id, workspace_id),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM workspaces WHERE family_id = ? ORDER BY last_seen_at DESC LIMIT 1",
                    (family_id,),
                ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def latest_for_file_object(root: Path, volume: str, file_id: str) -> dict[str, Any] | None:
    """The last thing this exact file instance was, if we ever saw it."""
    if not volume or not file_id:
        return None
    return _latest_observation(root, "volume = ? AND file_id = ?", (volume, file_id))


def latest_versioned_for_file_object(
    root: Path, volume: str, file_id: str, family_id: str
) -> dict[str, Any] | None:
    """The last *versioned* observation of that file instance for one family —
    the "which version is this actually based on" evidence a question carries."""
    if not volume or not file_id:
        return None
    return _latest_observation(
        root,
        "volume = ? AND file_id = ? AND family_id = ? AND version IS NOT NULL",
        (volume, file_id, family_id),
    )


def _latest_observation(root: Path, where: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    try:
        with _connect(root) as db:
            row = db.execute(
                f"SELECT * FROM observations WHERE {where} ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def known_sha256(root: Path, family_id: str, sha256: str) -> bool:
    try:
        with _connect(root) as db:
            row = db.execute(
                "SELECT 1 FROM observations WHERE family_id = ? AND sha256 = ? LIMIT 1",
                (family_id, sha256),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def families_for_hints(root: Path, hints: dict[str, str]) -> list[dict[str, Any]]:
    """Families whose *source* carried the same lineage hints (candidate only)."""
    wanted = {key: value for key, value in hints.items() if value}
    if not wanted:
        return []
    clauses, params = [], []
    for column in ("doc_id", "wps_hdid", "created"):
        if wanted.get(column):
            clauses.append(f"{column} = ?")
            params.append(wanted[column])
    if not clauses:
        return []
    try:
        with _connect(root) as db:
            rows = db.execute(
                f"SELECT family_id, MAX(observed_at) AS seen FROM observations"
                f" WHERE kind = 'source' AND ({' OR '.join(clauses)}) GROUP BY family_id"
                f" ORDER BY seen DESC",
                params,
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

def resolve(docx: str | Path, *, root: str | Path | None = None) -> dict[str, Any]:
    """Map one DOCX observation to a family, by proof before continuity.

    Returns ``{"status": "resolved" | "adoption-required" | "unbound" | ...}``
    with the reason and the candidates; the caller never guesses.
    """
    path = Path(docx)
    if not path.is_file():
        return {"status": "unbound", "reason": "file-not-found", "path": str(path)}
    cache = registry_path(root)
    sha256 = content_hash(path)
    volume, file_id = file_identity(path)

    exact = families_for_sha256(cache, sha256)
    if len(exact) == 1:
        entry = _workspace_state(cache, exact[0]["family_id"])
        return {"status": entry["status"], "reason": "exact-artifact", "sha256": sha256, **entry}
    if len(exact) > 1:
        candidates = [_workspace_state(cache, row["family_id"]) for row in exact]
        return {
            "status": "adoption-required",
            "reason": "ambiguous-exact-match",
            "sha256": sha256,
            "candidates": candidates,
        }

    latest = latest_for_file_object(cache, volume, file_id)
    if latest is not None:
        family_id = latest["family_id"]
        if known_sha256(cache, family_id, sha256):
            entry = _workspace_state(cache, family_id)
            return {
                "status": entry["status"],
                "reason": "local-file-object-with-known-content",
                "sha256": sha256,
                **entry,
            }
        versioned = latest_versioned_for_file_object(cache, volume, file_id, family_id) or latest
        return {
            "status": "adoption-required",
            "reason": "same-local-file-object",
            "sha256": sha256,
            "candidates": [
                {
                    **_workspace_state(cache, family_id),
                    "previous_version": versioned.get("version"),
                    "previous_sha256": versioned.get("sha256"),
                    "evidence": {
                        "same_file_id": True,
                        "known_exact_hash": False,
                        "docId_match": bool(latest.get("doc_id")),
                    },
                }
            ],
        }

    hints = lineage_hints(path)
    hinted = families_for_hints(cache, hints)
    if hinted:
        return {
            "status": "adoption-required",
            "reason": "document-metadata-hint",
            "sha256": sha256,
            "candidates": [
                {
                    **_workspace_state(cache, row["family_id"]),
                    "evidence": {
                        "same_file_id": False,
                        "known_exact_hash": False,
                        "docId_match": bool(hints.get("doc_id")),
                        "wps_hdid_match": bool(hints.get("wps_hdid")),
                    },
                }
                for row in hinted
            ],
        }

    return {"status": "unbound", "reason": "no-evidence", "sha256": sha256}


def _workspace_state(cache: Path, family_id: str) -> dict[str, Any]:
    """Where the family's workspace is, and whether it is usable on this machine."""
    record = workspace_for(cache, family_id)
    if record is None:
        return {
            "status": "family-known-but-workspace-missing",
            "family_id": family_id,
            "workspace": None,
            "note": "the family is known to the resolver but no workspace is recorded; locate or create one",
        }
    path = Path(record["path"]) if record.get("path") else None
    identity = read_identity(path) if path is not None and path.is_dir() else None
    if identity is None or identity.get("workspace_id") != record["workspace_id"]:
        return {
            "status": "family-known-but-workspace-missing",
            "family_id": family_id,
            "workspace": record.get("path") or None,
            "workspace_id": record["workspace_id"],
            "note": "the recorded workspace is not at that path any more; locate it or create a new one",
        }
    return {
        "status": "resolved",
        "family_id": family_id,
        "workspace_id": identity["workspace_id"],
        "workspace": str(path),
    }

def register_workspace(workdir: str | Path, *, root: str | Path | None = None) -> dict[str, Any]:
    """Mint-or-read a workdir's identity and put it in the cache. Returns the
    identity; the workspace file stays authoritative, so this is safe to call
    whenever the engine touches a workspace."""
    from .workspace_identity import ensure_identity

    identity, _created = ensure_identity(workdir)
    record_workspace(registry_path(root), identity, path=str(workdir))
    return identity


def register_export(
    workdir: str | Path,
    output: str | Path,
    *,
    version: str | None,
    kind: str = "managed-export",
    root: str | Path | None = None,
) -> None:
    """Record a DOCX the engine itself produced, so rename/copy/send-and-return
    all resolve back to this family by exact bytes."""
    path = Path(output)
    if not path.is_file():
        return
    identity = register_workspace(workdir, root=root)
    observe(
        registry_path(root),
        sha256=content_hash(path),
        family_id=identity["family_id"],
        workspace_id=identity["workspace_id"],
        version=version,
        kind=kind,
        path=path,
    )


def register_source(
    workdir: str | Path,
    source: str | Path,
    *,
    root: str | Path | None = None,
    origin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record the DOCX a workspace was extracted from (with its lineage hints)."""
    from .workspace_identity import ensure_identity

    path = Path(source)
    identity, _created = ensure_identity(workdir, origin=origin)
    record_workspace(registry_path(root), identity, path=str(workdir))
    if path.is_file():
        observe(
            registry_path(root),
            sha256=content_hash(path),
            family_id=identity["family_id"],
            workspace_id=identity["workspace_id"],
            kind="source",
            path=path,
            hints=lineage_hints(path),
        )
    return identity


def adopt(
    docx: str | Path,
    *,
    family_id: str,
    token: str,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Bind one observed DOCX to a family, once a human confirmed it.

    The token carries the evidence the question was asked about, so a file that
    changed while the question was on screen cannot be adopted by accident."""
    import base64 as _b64

    path = Path(docx)
    try:
        payload = json.loads(_b64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {"status": "refused", "reason": "adoption-token-invalid"}
    if not path.is_file():
        return {"status": "refused", "reason": "file-not-found", "path": str(path)}
    sha256 = content_hash(path)
    volume, file_id = file_identity(path)
    if payload.get("sha256") != sha256 or payload.get("volume") != volume or payload.get("file_id") != file_id:
        return {
            "status": "refused",
            "reason": "adoption-token-stale",
            "note": "the file changed while the question was open; re-run workdir_open on it",
        }
    state = _workspace_state(registry_path(root), family_id)
    if state["status"] != "resolved":
        return {"status": "refused", "reason": state["status"], **{k: v for k, v in state.items() if k != "status"}}
    observe(
        registry_path(root),
        sha256=sha256,
        family_id=family_id,
        workspace_id=state["workspace_id"],
        kind="adopted",
        path=path,
        hints=lineage_hints(path),
    )
    record_event(registry_path(root), "adopted", family_id=family_id,
                 workspace_id=state["workspace_id"], detail={"path": str(path), "sha256": sha256})
    return {"status": "resolved", "reason": "adopted", "family_id": family_id,
            "workspace_id": state["workspace_id"], "workspace": state["workspace"]}


def adoption_token(docx: str | Path) -> str:
    """Opaque evidence handle for one open question about one file instance."""
    import base64 as _b64

    path = Path(docx)
    volume, file_id = file_identity(path)
    payload = {
        "sha256": content_hash(path),
        "volume": volume,
        "file_id": file_id,
        "path": str(path),
    }
    return _b64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")

