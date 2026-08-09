from __future__ import annotations

import json

import pytest

from scripts.review_collab import (
    CollaborationError,
    document_state,
    ensure_session,
    external_write_guard,
    preflight,
    publish_current,
    settle_decisions,
    settlement_plan,
    stage_patch,
    writer_lane,
)
from scripts.review_queue import dispatch, snapshot, upsert_event


def _workdir(tmp_path):
    (tmp_path / "typed.md").write_text("base\n", encoding="utf-8")
    return tmp_path


def _patch(parent: str, *, after: str, client_id: str) -> dict[str, object]:
    return {
        "type": "patch",
        "client_id": client_id,
        "origin": "human_ui",
        "author": "Lin",
        "parent_snapshot": parent,
        "paragraph_id": "P16",
        "kind": "replace",
        "target": {
            "start_offset": 0,
            "end_offset": 4,
            "expected_text": "效果良好",
            "left_context": "",
            "right_context": "，但",
            "paragraph_fingerprint": "p16-fp",
            "region_fingerprint": "r16-fp",
            "style_region_ids": ["s_text"],
        },
        "before": "效果良好",
        "after": after,
    }


def test_staged_snapshot_chains_patches_without_advancing_canonical(tmp_path):
    workdir = _workdir(tmp_path)

    initial = ensure_session(workdir)
    first = stage_patch(workdir, _patch(initial["current_snapshot"]["id"], after="效果有限", client_id="p1"))
    second = stage_patch(workdir, _patch(first["staged_snapshot"], after="效果一般", client_id="p2"))

    state = document_state(workdir)
    assert state["review_base"]["id"] == "S0"
    assert state["current_snapshot"]["id"] == "C0"
    assert first["delivery_state"] == "staged"
    assert second["staged_parent_snapshot"] == first["staged_snapshot"]
    assert state["staged_snapshot"]["patch_ids"] == [first["event_id"], second["event_id"]]
    assert state["current_snapshot"]["typed_sha256"] == initial["current_snapshot"]["typed_sha256"]


def test_stale_staged_parent_is_rejected(tmp_path):
    workdir = _workdir(tmp_path)
    initial = ensure_session(workdir)
    stage_patch(workdir, _patch(initial["current_snapshot"]["id"], after="效果有限", client_id="p1"))

    with pytest.raises(CollaborationError, match="staged-parent-mismatch"):
        stage_patch(workdir, _patch(initial["current_snapshot"]["id"], after="效果一般", client_id="p2"))


def test_send_freezes_batch_and_preserves_event_state_separation(tmp_path):
    workdir = _workdir(tmp_path)
    initial = ensure_session(workdir)
    staged = stage_patch(workdir, _patch(initial["current_snapshot"]["id"], after="效果有限", client_id="p1"))

    queued = dispatch(workdir)
    assert len(queued) == 1
    event = queued[0]
    assert event["event_id"] == staged["event_id"]
    assert event["delivery_state"] == "queued"
    assert event["review_decision"] == "pending"
    assert event["batch_id"]
    assert snapshot(workdir)["counts"]["queued"] == 1

def test_agent_preflight_exposes_wake_queue_and_blocks_queued_human_patch(tmp_path):
    workdir = _workdir(tmp_path)
    initial = ensure_session(workdir)
    stage_patch(workdir, _patch(initial["current_snapshot"]["id"], after="效果有限", client_id="human-1"))
    dispatch(workdir)

    gate = preflight(workdir)
    assert gate["ready"] is False
    assert "queued-human-patch" in gate["reasons"]
    assert gate["queued_events"][0]["batch_id"]


def test_writer_lane_rejects_concurrent_canonical_transaction(tmp_path):
    workdir = _workdir(tmp_path)
    ensure_session(workdir)
    with writer_lane(workdir):
        with pytest.raises(CollaborationError, match="writer-busy"):
            with writer_lane(workdir):
                pass

def test_settlement_carries_stale_patch_forward_after_external_publish(tmp_path):
    workdir = _workdir(tmp_path)
    initial = ensure_session(workdir)
    staged = stage_patch(workdir, _patch(initial["current_snapshot"]["id"], after="效果有限", client_id="p1"))
    dispatch(workdir)
    workdir.joinpath("typed.md").write_text("external\n", encoding="utf-8")
    publish_current(
        workdir,
        expected_parent_snapshot=initial["current_snapshot"]["id"],
        origin="external",
        changed_paragraph_ids=["P16"],
    )

    plan = settlement_plan(workdir)
    assert plan["carry_forward"][0]["event_id"] == staged["event_id"]
    assert plan["ready_for_agent_write"] is False


def test_external_write_guard_fails_closed_on_filesystem_drift(tmp_path):
    workdir = _workdir(tmp_path)
    initial = ensure_session(workdir)
    guard = external_write_guard(
        workdir,
        expected_parent_snapshot=initial["current_snapshot"]["id"],
        operation="import",
    )
    assert guard["schema"] == "docx2typed-review-external-guard-1"
    workdir.joinpath("typed.md").write_text("drift\n", encoding="utf-8")
    with pytest.raises(CollaborationError, match="current-snapshot-drift"):
        external_write_guard(
            workdir,
            expected_parent_snapshot=initial["current_snapshot"]["id"],
            operation="rollback",
        )

def test_publish_current_uses_compare_and_swap_parent(tmp_path):
    workdir = _workdir(tmp_path)
    initial = ensure_session(workdir)
    expected = initial["current_snapshot"]["id"]
    workdir.joinpath("typed.md").write_text("changed\n", encoding="utf-8")

    published = publish_current(workdir, expected_parent_snapshot=expected, origin="human_ui", changed_paragraph_ids=["P16"])
    assert published["previous_snapshot"] == expected
    assert published["current_snapshot"]["id"] == "C1"

    with pytest.raises(CollaborationError, match="current-parent-mismatch"):
        publish_current(workdir, expected_parent_snapshot=expected, origin="agent", changed_paragraph_ids=["P16"])

    state = document_state(workdir)
    assert state["current_snapshot"]["id"] == "C1"
    assert state["current_snapshot"]["origin"] == "human_ui"
    assert json.loads((workdir / ".review" / "session.json").read_text(encoding="utf-8"))["current_snapshot"]["id"] == "C1"

def test_settle_mixed_decisions_advances_baseline_and_carries_defer(tmp_path):
    from tests.test_decisions import _key, extract_fixture

    workdir = extract_fixture(tmp_path)
    inventory = json.loads((workdir / "revisions.json").read_text(encoding="utf-8"))
    actions = {"100": "accept", "101": "reject", "102": "defer"}
    for revision in inventory["revisions"]:
        if revision["w_id"] not in actions:
            continue
        upsert_event(
            workdir,
            {
                "type": "decision",
                "client_id": f"settle:{revision['w_id']}",
                "paragraph_id": revision["paragraph_id"],
                "revision_id": revision["w_id"],
                "revision_key": revision["revision_key"],
                "selected_text": revision["text"],
                "decision": actions[revision["w_id"]],
                "comment": "",
            },
        )
    dispatch(workdir)

    result = settle_decisions(workdir)
    assert result["current_snapshot"]["id"] == "C1"
    assert result["review_base"]["id"] == "S1"
    assert {item["w_id"] for item in json.loads((workdir / "revisions.json").read_text(encoding="utf-8"))["revisions"]} == {"102", "103"}
    assert [item["revision_id"] for item in result["deferred"]] == ["102"]
    again = settle_decisions(workdir, result["settled_event_ids"])
    assert again["state"] == "already-settled"
