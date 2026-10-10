"""Delivery evidence: bind a published artifact to its verification.

P0-B-Evidence. ``build_docx`` already runs ``package_guard`` and an independent
``verify_workdir`` before publishing (ADR 0022), but the run-evidence payload
did not say which checks ran, against which immutable version, for which
artifact bytes. These tests pin the added ``docx2typed-delivery-1`` record and
the delivery status machine a later gate will consume.

Three independent layers, per review:
  1. the record is present and well formed in the build evidence;
  2. it binds to artifact bytes + source version, not to a path;
  3. freshness is derived from the artifact hash, so a later workspace edit
     does not invalidate an unchanged artifact, but editing the artifact does.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from docx import Document

from scripts.extract import extract
from scripts.protocol import (
    DELIVERY_POLICY_VERSION,
    DELIVERY_SCHEMA,
    delivery_record,
    delivery_status,
)
from scripts.mcp_server import (
    build_docx,
    commit_sync,
    document_patch,
    session,
    workdir_open,
)


def _reset() -> None:
    session.workdir = None
    session.last_build_output = None


def _data(result) -> dict:
    """The data dict from a Result envelope's structuredContent."""
    return result.structuredContent["data"]


def _open(tmp_path: Path, name: str) -> Path:
    source = tmp_path / f"{name}-src.docx"
    workdir = tmp_path / name
    document = Document()
    document.add_paragraph("初始文本 ABC")
    document.save(source)
    assert extract([str(source), "-o", str(workdir)]) == 0
    return Path(json.loads(workdir_open(str(workdir)))["workdir"])


def _evidence(output: Path) -> dict:
    return json.loads(Path(str(output) + ".evidence.json").read_text(encoding="utf-8"))


def _delivery_of(output: Path) -> dict:
    return _evidence(output)["payload"]["delivery"]


def _build(tmp_path: Path, name: str) -> tuple[Path, dict]:
    """Open a workdir, save it, build it, and return (output, delivery)."""
    _reset()
    _open(tmp_path, name)
    _data(commit_sync(operation_id=f"{name}-save"))
    output = Path(_data(build_docx(operation_id=f"{name}-build"))["output"])
    return output, _delivery_of(output)


# --------------------------------------------------------------------------
# Layer 1 — the record exists and is well formed
# --------------------------------------------------------------------------


def test_build_evidence_carries_a_delivery_record(tmp_path):
    output, delivery = _build(tmp_path, "delivery")

    assert delivery["schema"] == DELIVERY_SCHEMA
    assert delivery["artifact_sha256"] == _evidence(output)["payload"]["outputs"]["docx"]["sha256"]
    assert delivery["verification"] == {
        "methods": ["package_guard", "verify_workdir"],
        "verdict": "pass",
        "policy_version": DELIVERY_POLICY_VERSION,
    }
    # the source binding names an immutable version + tree, not a path
    source = delivery["source"]
    assert source["version_id"] and source["tree_sha256"]
    assert "path" not in source
    assert str(tmp_path) not in json.dumps(delivery)


def test_methods_only_name_checks_that_ran(tmp_path):
    """The delivery record must not be padded from another record's shape."""
    _, delivery = _build(tmp_path, "methods")
    methods = delivery["verification"]["methods"]
    assert methods == ["package_guard", "verify_workdir"]
    # verify_output's six interpretation labels are NOT delivery methods
    for label in ("text", "styles", "structure", "package", "revisions", "comments"):
        assert label not in methods


# --------------------------------------------------------------------------
# Layer 2 — the binding is to bytes + version, not to a path
# --------------------------------------------------------------------------


def test_record_binds_to_content_not_path(tmp_path):
    output, delivery = _build(tmp_path, "binding")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert delivery["artifact_sha256"] == digest
    # same bytes under a different name still resolve to the verified state
    assert delivery_status(delivery, artifact_sha256=digest) == "verified-current"


# --------------------------------------------------------------------------
# Layer 3 — freshness is byte-derived
# --------------------------------------------------------------------------


def test_editing_the_artifact_after_build_invalidates_the_verdict(tmp_path):
    output, delivery = _build(tmp_path, "stale")
    before = hashlib.sha256(output.read_bytes()).hexdigest()
    assert delivery_status(delivery, artifact_sha256=before) == "verified-current"

    # simulate a change to the delivered bytes
    output.write_bytes(output.read_bytes() + b" ")
    after = hashlib.sha256(output.read_bytes()).hexdigest()
    assert after != before
    assert delivery_status(delivery, artifact_sha256=after) == "modified-after-verify"


def test_later_workspace_edit_does_not_invalidate_an_earlier_artifact(tmp_path):
    """HEAD moving forward must not stale an unchanged delivered artifact."""
    output, delivery = _build(tmp_path, "advanced")

    # advance the workspace: another edit + another save
    _data(
        document_patch(
            hunks=[{"paragraph_id": "P0", "old": "初始文本 ABC", "new": "改后文本 XYZ"}],
            operation_id="advanced-edit",
        )
    )
    _data(commit_sync(operation_id="advanced-save-2"))

    # the earlier artifact was not rebuilt, and its bytes did not change
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert digest == delivery["artifact_sha256"]
    assert delivery_status(delivery, artifact_sha256=digest) == "verified-current"


# --------------------------------------------------------------------------
# The status machine itself
# --------------------------------------------------------------------------


def test_delivery_status_machine_states():
    record = delivery_record(
        artifact_sha256="a" * 64,
        methods=["package_guard", "verify_workdir"],
        verdict="pass",
        family_id="f_x",
        version_id="V1",
        source_tree_sha256="t",
    )
    assert delivery_status(record, artifact_sha256="a" * 64) == "verified-current"
    assert delivery_status(record, artifact_sha256="b" * 64) == "modified-after-verify"
    assert delivery_status(record, artifact_sha256=None) == "missing"
    assert delivery_status(None, artifact_sha256="a" * 64) == "unregistered"
    assert delivery_status({"schema": "other"}, artifact_sha256="a" * 64) == "unregistered"


def test_delivery_record_rejects_an_invalid_verdict():
    with pytest.raises(ValueError):
        delivery_record(
            artifact_sha256="a" * 64,
            methods=["package_guard"],
            verdict="maybe",
            family_id=None,
            version_id=None,
            source_tree_sha256=None,
        )
