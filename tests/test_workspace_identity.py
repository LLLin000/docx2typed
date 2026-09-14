"""docx2typed-mcp: workspace identity and the resolver's evidence ladder.

The contract under test (design 2026-09-14): a DOCX is an *observation* of a
family, so "which workspace is this?" is answered by proof — exact bytes, then
a known local file object, then metadata hints that may only nominate a
candidate. Anything short of proof becomes one question, never a guess.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from docx import Document

from scripts import main
from scripts.mcp_server import (
    _workdir_open_result,
    build_docx,
    commit_sync,
    session,
    workdir_open,
)
from scripts.workspace_identity import ensure_identity, read_identity
from scripts.workspace_registry import (
    content_hash,
    file_identity,
    registry_path,
    resolve,
)


def _write_docx(path: Path, text: str = "肩袖再撕裂是肩袖修复术后常见并发症。") -> Path:
    document = Document()
    document.add_paragraph(text)
    document.save(path)
    return path


def _cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "registry.sqlite3"
    monkeypatch.setenv("DOCX2TYPED_REGISTRY", str(cache))
    return cache


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    source = _write_docx(tmp_path / "screw.docx")
    workdir = tmp_path / "wd"
    assert main(["--json", "extract", str(source), "-o", str(workdir), "--operation-id", "e1"]) == 0
    return source, workdir


def _envelope(result):
    if isinstance(result, str):
        return json.loads(result)
    if isinstance(result, dict):
        return result
    return result.structuredContent


def _open(path: Path, **kwargs) -> dict:
    return _envelope(_workdir_open_result(str(path), **kwargs))


def _open_session(workdir: Path) -> None:
    """Open one workdir as the session document (the tools act on it)."""
    session.workdir = None
    _open(workdir, track=False)


def _rewrite_in_place(path: Path, extra: str) -> None:
    """Rewrite word/document.xml the way Word saving would: same file object,
    different bytes."""
    with zipfile.ZipFile(path) as archive:
        members = [(info, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in members:
            if info.filename == "word/document.xml":
                data = data.decode("utf-8").replace(
                    "</w:body>", f"<w:p><w:r><w:t>{extra}</w:t></w:r></w:p></w:body>", 1
                ).encode("utf-8")
            archive.writestr(info, data)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def test_extract_mints_identity_once(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    identity = read_identity(workdir)
    assert identity is not None
    assert identity["family_id"].startswith("f_")
    assert identity["workspace_id"].startswith("ws_")
    assert identity["origin_family"] is None
    again, created = ensure_identity(workdir)
    assert created is False
    assert again["family_id"] == identity["family_id"]


def test_session_descriptor_carries_the_ids(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    session.workdir = None
    opened = _open(workdir, track=False)
    identity = read_identity(workdir)
    assert opened["data"]["session"]["workspace"]["family_id"] == identity["family_id"]
    assert opened["data"]["session"]["workspace"]["workspace_id"] == identity["workspace_id"]


# --------------------------------------------------------------------------
# Proof tiers: auto-resolve
# --------------------------------------------------------------------------

def test_exact_bytes_resolve_across_rename_and_copy(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    source, workdir = _workspace(tmp_path)
    identity = read_identity(workdir)
    _open_session(workdir)
    commit_sync(label="V1")
    export = tmp_path / "导出.docx"
    build_docx(output=str(export))

    renamed = tmp_path / "老师二审.docx"
    export.rename(renamed)
    copied = tmp_path / "发出去的副本.docx"
    copied.write_bytes(renamed.read_bytes())

    for observation in (source, renamed, copied):
        result = resolve(observation)
        assert result["status"] == "resolved"
        assert result["reason"] == "exact-artifact"
        assert result["family_id"] == identity["family_id"]
        assert Path(result["workspace"]) == workdir.resolve()


def test_workdir_open_follows_a_document_path_to_its_workspace(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    _open_session(workdir)
    commit_sync(label="V1")
    export = tmp_path / "导出.docx"
    build_docx(output=str(export))
    sent = tmp_path / "发给合作者.docx"
    export.rename(sent)

    session.workdir = None
    opened = json.loads(workdir_open(str(sent), track=False))
    assert Path(opened["workdir"]) == workdir.resolve()


# --------------------------------------------------------------------------
# Continuity tiers: ask exactly once
# --------------------------------------------------------------------------

def test_edited_file_object_asks_once_and_carries_its_previous_version(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    identity = read_identity(workdir)
    _open_session(workdir)
    commit_sync(label="V1")
    export = tmp_path / "导出.docx"
    build_docx(output=str(export))

    before = file_identity(export)
    _rewrite_in_place(export, "老师又加了一句。")
    if file_identity(export) != before:
        pytest.skip("this filesystem does not keep a stable file identity across an in-place write")

    result = resolve(export)
    assert result["status"] == "adoption-required"
    assert result["reason"] == "same-local-file-object"
    candidate = result["candidates"][0]
    assert candidate["family_id"] == identity["family_id"]
    assert candidate["previous_version"] == "V1"
    assert candidate["evidence"] == {
        "same_file_id": True,
        "known_exact_hash": False,
        "docId_match": False,
    }


def test_unknown_document_is_unbound_not_guessed(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _workspace(tmp_path)
    stranger = _write_docx(tmp_path / "别的课题.docx", "完全无关的一份文档。")
    result = resolve(stranger)
    assert result["status"] == "unbound"
    assert result["reason"] == "no-evidence"

    session.workdir = None
    failure = _open(stranger, track=False)
    diagnostic = failure["diagnostics"][0]
    assert diagnostic["code"] == "workspace-unbound"
    assert diagnostic["details"]["actions"] == ["create-workspace", "choose-existing-workspace"]


def test_adopt_binds_the_content_and_then_stops_asking(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    _open_session(workdir)
    commit_sync(label="V1")
    export = tmp_path / "导出.docx"
    build_docx(output=str(export))
    _rewrite_in_place(export, "老师又加了一句。")

    session.workdir = None
    failure = _open(export, track=False)
    details = failure["diagnostics"][0]["details"]
    assert failure["diagnostics"][0]["code"] == "workspace-adoption-required"

    session.workdir = None
    _open(workdir, track=False)
    from scripts.mcp_server import workspace_adopt

    adopted = _envelope(workspace_adopt(token=details["adoption_token"], family_id=details["candidates"][0]["family_id"]))
    assert adopted["data"]["workspace_id"] == read_identity(workdir)["workspace_id"]

    session.workdir = None
    after = _open(export, track=False)
    assert after["data"]["session"]["workspace"]["workspace_id"] == read_identity(workdir)["workspace_id"]


def test_adoption_token_is_refused_when_the_file_changed_meanwhile(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    _open_session(workdir)
    commit_sync(label="V1")
    export = tmp_path / "导出.docx"
    build_docx(output=str(export))
    _rewrite_in_place(export, "第一处修改。")

    session.workdir = None
    details = _open(export, track=False)["diagnostics"][0]["details"]
    token = details["adoption_token"]
    _rewrite_in_place(export, "第二处修改。")  # 问题还开着，文件又变了

    session.workdir = None
    _open(workdir, track=False)
    from scripts.mcp_server import workspace_adopt

    refused = _envelope(workspace_adopt(token=token, family_id=details["candidates"][0]["family_id"]))
    assert refused["diagnostics"][0]["code"] == "adoption-token-stale"


def test_ambiguous_exact_bytes_ask_instead_of_guessing(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    source, workdir = _workspace(tmp_path)
    _open_session(workdir)
    commit_sync(label="V1")
    export = tmp_path / "导出.docx"
    build_docx(output=str(export))

    # a fork claims the very same bytes as its own family: two owners, one hash
    from scripts.mcp_server import workspace_fork

    forked = _envelope(workspace_fork(docx=str(export), outdir=str(tmp_path / "wd2")))
    assert forked["data"]["family_id"] != read_identity(workdir)["family_id"]
    assert forked["data"]["origin_family"] == read_identity(workdir)["family_id"]

    result = resolve(export)
    assert result["status"] == "adoption-required"
    assert result["reason"] == "ambiguous-exact-match"
    assert {candidate["family_id"] for candidate in result["candidates"]} == {
        read_identity(workdir)["family_id"],
        forked["data"]["family_id"],
    }

    session.workdir = None
    failure = _open(export, track=False)
    assert failure["diagnostics"][0]["code"] == "workspace-adoption-required"
    assert content_hash(source) != content_hash(tmp_path / "wd2" / "typed.md")


def test_fork_keeps_the_origin_for_audit_only(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    origin = read_identity(workdir)
    session.workdir = None
    from scripts.mcp_server import workspace_fork

    forked = _envelope(workspace_fork(docx=str(tmp_path / "screw.docx"), outdir=str(tmp_path / "patent")))
    identity = read_identity(tmp_path / "patent")
    assert identity["family_id"] != origin["family_id"]
    assert identity["origin_family"] == origin["family_id"]
    assert forked["data"]["status"] == "forked"


def test_recorded_workspace_that_moved_is_reported(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    moved = tmp_path / "挪走的工作区"
    workdir.rename(moved)

    result = resolve(tmp_path / "screw.docx")
    assert result["status"] == "family-known-but-workspace-missing"
    assert result["workspace_id"] == read_identity(moved)["workspace_id"]

    session.workdir = None
    failure = _open(tmp_path / "screw.docx", track=False)
    assert failure["diagnostics"][0]["code"] == "workspace-workspace-missing"


def test_deleting_the_registry_is_survivable(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _, workdir = _workspace(tmp_path)
    identity = read_identity(workdir)
    cache.unlink()

    # the workspace file is authoritative: the workdir still opens and still
    # carries its ids, and the cold cache simply asks again
    session.workdir = None
    opened = _open(workdir, track=False)
    assert opened["data"]["session"]["workspace"]["family_id"] == identity["family_id"]
    assert resolve(tmp_path / "screw.docx")["status"] == "unbound"
    assert registry_path() == cache
    assert os.environ.get("DOCX2TYPED_REGISTRY") == str(cache)
