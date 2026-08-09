from __future__ import annotations

from types import SimpleNamespace

import pytest

import scripts.review_server as review_server
from scripts import main as cli_main
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


def test_tailscale_ipv4_reads_the_first_ipv4(monkeypatch):
    monkeypatch.setattr(review_server.shutil, "which", lambda name: "tailscale")
    monkeypatch.setattr(
        review_server.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="100.64.0.7\nfd7a::1\n"),
    )

    assert review_server._tailscale_ipv4() == "100.64.0.7"


def test_tailscale_ipv4_fails_closed_when_cli_is_missing(monkeypatch):
    monkeypatch.setattr(review_server.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="tailscale-not-installed"):
        review_server._tailscale_ipv4()


def test_cli_dispatches_mcp_and_review_subcommands(monkeypatch):
    import scripts.mcp_server as mcp_server

    mcp_calls = []
    review_calls = []
    monkeypatch.setattr(mcp_server, "main", lambda: mcp_calls.append(True))
    monkeypatch.setattr(review_server, "main", lambda argv: review_calls.append(argv) or 0)

    assert cli_main(["mcp"]) == 0
    assert cli_main(["review", "workdir", "--tailscale"]) == 0
    assert mcp_calls == [True]
    assert review_calls == [["workdir", "--tailscale"]]
