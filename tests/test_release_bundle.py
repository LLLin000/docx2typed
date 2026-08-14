"""Focused tests for the signed Reference bundle publisher (issue #54).

Covers the load-bearing contracts with synthetic runs (no plan execution):
the immutable bundle layout, the detached Ed25519 signature and its
verification (including tamper detection), Semantic-root equality across two
runs, and Oracle-freeze enforcement (drift fails closed until a classified
new major).  The bundle is published into tmp_path; the repository itself is
never written by these tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.release_bundle as rb
from scripts.qualify import (
    _execute_oracle_freeze_check,
    canonical_verdict,
    plan_sha256,
    validate_identities,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_plan() -> dict:
    return json.loads((ROOT / "qualification" / "plan.json").read_text(encoding="utf-8"))


def _synthetic_run(plan: dict, index: int, report_dir: Path) -> dict:
    """A minimal run: report/verdict artifacts plus canonical artifacts, so
    publish/verify logic is exercised without executing the frozen plan."""
    plan_sha = plan_sha256(plan)
    checks = [
        {"id": "identity", "kind": "identity", "bindings": {}, "result": "pass", "detail": "", "ops": []},
        {"id": "noop-bytes", "kind": "noop_bytes", "bindings": {}, "result": "pass", "detail": "", "ops": []},
        {"id": "oracle-freeze", "kind": "oracle_freeze", "bindings": {}, "result": "pass", "detail": "vacuous", "ops": []},
        {"id": "self-comparison", "kind": "self_comparison", "bindings": {}, "result": "pass", "detail": "identical", "ops": []},
    ]
    report = {
        "schema": "docx2typed-qualification-report-1",
        "canon": "docx2typed-qual-canon-1",
        "plan_sha256": plan_sha,
        "generated": "2026-08-14T00:00:00+00:00",
        "identities": {},
        "checks": checks,
        "summary": {},
        "verdict": {"result": "fail", "reason": "office-evidence blocked-not-run"},
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(json.dumps(report) + "\n", encoding="utf-8")
    (report_dir / "verdict.json").write_text(json.dumps(report["verdict"]) + "\n", encoding="utf-8")
    identities = validate_identities(plan, ROOT)
    cv = canonical_verdict(plan_sha, checks)
    digest, record = rb.compute_semantic_root(plan_sha, checks, identities, rb.evidence_hashes(plan))
    return {
        "index": index,
        "report": report,
        "plan_sha": plan_sha,
        "canonical_verdict": cv,
        "canonical_verdict_sha256": rb._sha256_bytes(rb.canonical_json(cv)),
        "identities": identities,
        "evidence": rb.evidence_hashes(plan),
        "semantic_root": digest,
        "semantic_root_record": record,
        "report_sha256": rb._file_sha256(report_dir / "report.json"),
        "report_dir": report_dir,
    }


class _Options:
    oracle_major = None
    classified = None
    tag = "v0.0.0-test"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Publish into tmp_path/reference/bundle-1 with a fresh dev key.  The
    archived inputs still come from the real repository; the freeze chain is
    isolated so tests do not depend on committed bundles."""
    monkeypatch.setattr(rb, "DEFAULT_PLAN", tmp_path / "plan.json")
    (tmp_path / "plan.json").write_text(json.dumps(_load_plan()), encoding="utf-8")
    monkeypatch.setattr(rb, "KEYSTORE_DIR", tmp_path / "keystore")
    monkeypatch.setattr(rb, "KEYS_DIR", tmp_path / "keys")
    monkeypatch.setattr(rb, "DEV_PRIVKEY", tmp_path / "keystore" / "dev-signing.key")
    monkeypatch.setattr(rb, "DEV_PUBKEY", tmp_path / "keys" / "dev-signing-pub.pem")
    monkeypatch.setattr(rb, "OPERATOR_PUBKEY", tmp_path / "keys" / "release-signing-pub.pem")
    rb.init_dev_key()
    monkeypatch.setattr(rb, "next_bundle_dir", lambda root=rb.BUNDLE_ROOT: tmp_path / "reference" / "bundle-1")
    monkeypatch.setattr(rb, "latest_freeze_record", lambda root=rb.REPO_ROOT: None)
    return tmp_path


def test_bundle_publish_sign_verify_and_tamper_detection(isolated, tmp_path):
    plan = _load_plan()
    run_1 = _synthetic_run(plan, 1, tmp_path / "run-1/report")
    run_2 = _synthetic_run(plan, 2, tmp_path / "run-2/report")
    result = rb.publish_bundle(plan, [run_1, run_2], _Options())
    bundle = result["bundle_dir"]

    assert bundle.name == "bundle-1"
    assert run_1["semantic_root"] == run_2["semantic_root"]
    assert result["semantic_root"] == run_1["semantic_root"]
    assert result["signing"]["key_role"] == "dev"
    assert result["verification"]["ok"]

    # The published bundle audits cleanly and the signature binds it.
    assert rb.verify_bundle(bundle)["ok"]
    freeze = json.loads((bundle / "freeze.json").read_text(encoding="utf-8"))
    assert freeze["schema"] == rb.FREEZE_SCHEMA
    assert freeze["frozen"]["semantic_root_sha256"] == run_1["semantic_root"]
    assert freeze["history"][0]["decision"] == "initial Oracle freeze"

    # Any tamper with the canonical artifacts breaks verification.
    tampered = bundle / "runs/run-1/report.json"
    original = tampered.read_bytes()
    tampered.write_bytes(original + b" ")
    assert not rb.verify_bundle(bundle)["ok"]
    tampered.write_bytes(original)
    assert rb.verify_bundle(bundle)["ok"]

    # The detached signature covers the manifest: editing it invalidates.
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["commit"] = "tampered"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert not rb.verify_bundle(bundle)["ok"]


def test_oracle_freeze_enforcement_drift_fails_closed(isolated, tmp_path):
    plan = _load_plan()
    run_1 = _synthetic_run(plan, 1, tmp_path / "run-1/report")
    run_2 = _synthetic_run(plan, 2, tmp_path / "run-2/report")
    rb.publish_bundle(plan, [run_1, run_2], _Options())

    # Unchanged identities pass the check (the freeze record pins them).
    check = _execute_oracle_freeze_check(plan, isolated)
    assert check["result"] == "pass"

    # Any pinned identity drift fails closed.
    drifted = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    drifted["identities"]["capability"]["sha256"] = "0" * 64
    check = _execute_oracle_freeze_check(drifted, isolated)
    assert check["result"] == "fail"
    assert "capability" in check["detail"]
    assert "new Oracle major" in check["detail"]
