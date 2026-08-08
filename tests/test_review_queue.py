from __future__ import annotations

import pytest

from scripts.review_queue import acknowledge, dispatch, list_events, snapshot, upsert_event


def test_review_events_require_explicit_dispatch_and_ack(tmp_path):
    decision = upsert_event(
        tmp_path,
        {
            "type": "decision",
            "client_id": "decision:N36",
            "revision_id": "N36",
            "revision_key": "word/document.xml|delete|4|abc",
            "paragraph_id": "P16",
            "selected_text": "旧文本",
            "decision": "reject",
            "comment": "保留原文",
        },
    )
    updated = upsert_event(
        tmp_path,
        {
            "type": "decision",
            "client_id": "decision:N36",
            "revision_id": "N36",
            "revision_key": "word/document.xml|delete|4|abc",
            "paragraph_id": "P16",
            "selected_text": "旧文本",
            "decision": "accept",
            "comment": "改为接受",
        },
    )
    comment = upsert_event(
        tmp_path,
        {
            "type": "comment",
            "client_id": "comment:one",
            "paragraph_id": "P16",
            "selected_text": "一句话",
            "before_context": "前文",
            "after_context": "后文",
            "note": "请核对事实依据",
        },
    )

    assert decision["event_id"] == updated["event_id"]
    assert updated["status"] == "draft"
    assert comment["status"] == "draft"
    assert snapshot(tmp_path)["counts"] == {"draft": 2, "queued": 0, "acknowledged": 0}

    queued = dispatch(tmp_path)
    assert {event["event_id"] for event in queued} == {updated["event_id"], comment["event_id"]}
    assert len([event for event in list_events(tmp_path) if event["status"] == "queued"]) == 2

    acknowledged = acknowledge(tmp_path, [event["event_id"] for event in queued])
    assert len(acknowledged) == 2
    assert snapshot(tmp_path)["counts"] == {"draft": 0, "queued": 0, "acknowledged": 2}


def test_review_event_validation_rejects_unsupported_payload(tmp_path):
    with pytest.raises(ValueError, match="type must be decision or comment"):
        upsert_event(tmp_path, {"type": "instruction", "client_id": "bad"})

    with pytest.raises(ValueError, match="comment requires selected_text and note"):
        upsert_event(tmp_path, {"type": "comment", "client_id": "missing-note", "selected_text": "x"})
