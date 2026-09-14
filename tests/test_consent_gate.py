"""docx2typed: settling a revision is the user's decision.

Track-changes mode never accepts anything by itself, so the tools that settle
revisions (`accept_revision`, `reject_revision`, `decide_all`) require the
caller to state that the user confirmed it — and every refusal that used to
suggest "settle the revision first" now points at the user instead, because
advice is what an agent follows.
"""
from __future__ import annotations

import json
from pathlib import Path

from docx import Document

from scripts import main
from scripts.mcp_server import (
    accept_revision,
    commit_sync,
    decide_all,
    document_patch,
    reject_revision,
    session,
    workdir_open,
)
from scripts.typed_docx import validate_workdir


def _envelope(result):
    if isinstance(result, str):
        return json.loads(result)
    if isinstance(result, dict):
        return result
    return result.structuredContent


def _code(result) -> str:
    envelope = _envelope(result)
    return (envelope.get("diagnostics") or [{}])[0].get("code") or "OK"


def _revision_workdir(tmp_path: Path) -> tuple[Path, str, str]:
    """A workdir holding one tracked edit, and its revision key/fingerprint."""
    source = tmp_path / "s.docx"
    document = Document()
    document.add_paragraph("第一段正文。")
    document.save(str(source))
    workdir = tmp_path / "wd"
    assert main(["--json", "extract", str(source), "-o", str(workdir), "--operation-id", "e1"]) == 0
    session.workdir = None
    workdir_open(str(workdir), track=True)
    assert _code(document_patch(
        hunks=[{"paragraph_id": "P0", "old": "第一段正文。", "new": "第一段正文（改）。"}]
    )) == "OK"
    assert _code(commit_sync(label="改一段")) == "OK"
    revisions = json.loads((workdir / "revisions.json").read_text(encoding="utf-8"))
    items = revisions if isinstance(revisions, list) else (revisions.get("revisions") or [])
    entry = next(item for item in items if item.get("kind") == "insert")
    return workdir, entry["revision_key"], entry["revision_key"].split("|")[-1]


def test_accepting_without_the_users_confirmation_is_refused(tmp_path):
    workdir, key, fingerprint = _revision_workdir(tmp_path)
    before = (workdir / "typed.md").read_text(encoding="utf-8")

    result = accept_revision(key, fingerprint)
    assert _code(result) == "consent-required"
    message = (_envelope(result).get("diagnostics") or [{}])[0].get("message", "")
    assert "user" in message.lower()
    assert (workdir / "typed.md").read_text(encoding="utf-8") == before
    validate_workdir(workdir)


def test_accepting_with_confirmation_settles_the_revision(tmp_path):
    workdir, key, fingerprint = _revision_workdir(tmp_path)
    assert _code(accept_revision(key, fingerprint, user_confirmed=True)) == "OK"
    validate_workdir(workdir)


def test_rejecting_and_deciding_all_are_gated_the_same_way(tmp_path):
    workdir, key, fingerprint = _revision_workdir(tmp_path)
    assert _code(reject_revision(key, fingerprint)) == "consent-required"
    assert _code(decide_all("accept", str(tmp_path / "out.docx"))) == "consent-required"
    validate_workdir(workdir)
