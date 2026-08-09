from __future__ import annotations

from scripts.review_collab import CollaborationError
from scripts.review_server import _error_payload


def test_error_payload_preserves_stable_code_and_human_detail():
    assert _error_payload(CollaborationError("patch-context-mismatch", "P0: left context changed")) == {
        "error": "P0: left context changed",
        "code": "patch-context-mismatch",
    }


def test_error_payload_extracts_code_from_legacy_error_text():
    assert _error_payload(ValueError("writer-busy: another writer is active")) == {
        "error": "another writer is active",
        "code": "writer-busy",
    }
