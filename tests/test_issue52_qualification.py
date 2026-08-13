"""Issue #52: corpus manifest, resource profiles, and office evidence.

Covers the new governance/enforcement surfaces:
- corpus/manifest.json (docx2typed-fixture-manifest-2) validates structurally
  and every committed fixture hash matches disk;
- resource_profiles.json validates and the fail-closed gate rejects
  just-over packages with resource-limit-exceeded (actual/limit/profile)
  while just-inside packages pass and no partial output is published;
- the office-evidence blocking gate fails closed on not-run cells and the
  semantic retention rules apply named consumer-owned rewrite tolerances.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from scripts.fixture_manifest import ManifestError, load_manifest, validate_manifest
from scripts.office_evidence import (
    CONSUMER_REWRITE_RULES,
    CONSUMERS,
    evaluate_retention,
    missing_blocking_cells,
    semantic_signature,
    summarize_blocking,
    verify,
)
from scripts.resource_limits import (
    ProfileError,
    enforce_package,
    load_profiles,
    validate_profiles,
    violations,
)


# ---------------------------------------------------------------------------
# Fixture corpus manifest
# ---------------------------------------------------------------------------


def test_committed_fixture_manifest_is_valid_and_content_addressed():
    manifest = load_manifest(ROOT)
    detail = validate_manifest(manifest, ROOT, check_hashes=True)
    assert detail["valid"], detail
    tiers = {}
    for entry in manifest["fixtures"]:
        tiers[entry["tier"]] = tiers.get(entry["tier"], 0) + 1
    assert tiers.get("public", 0) >= 15
    assert tiers.get("calibration", 0) >= 11
    # high-compression-duplicate is covered at runtime by resource_limits
    # (STORED committed archives cannot reproduce a compression-ratio probe).
    assert "high-compression-duplicate" in manifest["coverage"]["runtime_corruptions"]


def test_fixture_manifest_drift_is_rejected(tmp_path):
    manifest = load_manifest(ROOT)
    manifest["fixtures"][0]["tier"] = "bogus"
    with pytest.raises(ManifestError, match="tier"):
        validate_manifest(manifest, ROOT, check_hashes=False)


# ---------------------------------------------------------------------------
# Fixture-corpus regeneration gate (byte-identity vs committed)
# ---------------------------------------------------------------------------


def _fake_committed_corpus(root: Path, *, manifest_text: str, model_text: str) -> None:
    (root / "corpus" / "release").mkdir(parents=True)
    (root / "corpus" / "manifest.json").write_text(manifest_text, encoding="utf-8")
    (root / "corpus" / "release" / "model-manifest.json").write_text(model_text, encoding="utf-8")


def _fake_regeneration(monkeypatch, *, manifest_text: str, model_text: str) -> None:
    """Replace the release_fixtures entrypoint with a deterministic fake that
    writes known bytes into the scratch dir, so the byte-compare logic under
    test is exercised without a full fixture regeneration."""
    import scripts.release_fixtures as rf

    def fake_generate(outdir):
        Path(outdir).mkdir(parents=True, exist_ok=True)
        return Path(outdir)

    def fake_check_models(release_dir, work_root, *, write=False):
        assert write
        Path(release_dir).mkdir(parents=True, exist_ok=True)
        (Path(release_dir) / "model-manifest.json").write_text(model_text, encoding="utf-8")
        return 0

    def fake_write_manifest(release_dir, calibration_dir, with_calibration=None, manifest_path="corpus/manifest.json", corpus_root=None):
        Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(manifest_path).write_text(manifest_text, encoding="utf-8")
        return Path(manifest_path)

    monkeypatch.setattr(rf, "generate", fake_generate)
    monkeypatch.setattr(rf, "generate_calibration", fake_generate)
    monkeypatch.setattr(rf, "check_models", fake_check_models)
    monkeypatch.setattr(rf, "write_manifest", fake_write_manifest)


_MANIFEST = '{"schema": "docx2typed-fixture-manifest-2", "version": 2, "generated": "2026-08-13", "tiers": {}, "fixtures": [], "coverage": {}}\n'
_MODEL = '{"schema": "docx2typed-fixture-model-1", "fixtures": {}}\n'


def test_fixture_corpus_gate_fails_when_regeneration_differs(tmp_path, monkeypatch):
    from scripts.release_fixtures import verify_corpus

    root = tmp_path / "root"
    _fake_committed_corpus(root, manifest_text=_MANIFEST, model_text=_MODEL)
    drifted = _MANIFEST.replace('"fixtures": []', '"fixtures": [{"path": "release/plain.docx", "sha256": "beef"}]')
    _fake_regeneration(monkeypatch, manifest_text=drifted, model_text=_MODEL.replace('"fixtures": {}', '"fixtures": {"plain.docx": "cafe"}'))
    result = verify_corpus(root, tmp_path / "work", tmp_path / "scratch")
    assert result["ok"] is False
    assert any("manifest.json differs" in diff for diff in result["diffs"])
    assert any("model-manifest.json differs" in diff for diff in result["diffs"])
    assert result["detail"].startswith("regeneration differs from committed")


def test_fixture_corpus_gate_passes_with_deterministic_regeneration(tmp_path, monkeypatch):
    from scripts.release_fixtures import verify_corpus

    root = tmp_path / "root"
    _fake_committed_corpus(root, manifest_text=_MANIFEST, model_text=_MODEL)
    _fake_regeneration(monkeypatch, manifest_text=_MANIFEST, model_text=_MODEL)
    result = verify_corpus(root, tmp_path / "work", tmp_path / "scratch")
    assert result["ok"] is True
    assert result["diffs"] == []


def test_committed_corpus_regenerates_byte_identical(tmp_path):
    """The real regeneration pipeline is deterministic: regenerating the
    release fixtures, typed-model manifest and corpus manifest into a temp
    dir reproduces the committed files byte-for-byte."""
    from scripts.release_fixtures import verify_corpus

    result = verify_corpus(ROOT, tmp_path / "work", tmp_path / "scratch")
    assert result["ok"] is True, result["detail"]


# ---------------------------------------------------------------------------
# Resource profiles + fail-closed enforcement
# ---------------------------------------------------------------------------


def test_resource_profiles_document_validates():
    profiles = validate_profiles(load_profiles(ROOT / "qualification" / "resource_profiles.json"))
    assert set(profiles["profiles"]) == {"S", "L", "X"}
    assert profiles["fail_closed"]["diagnostic"] == "resource-limit-exceeded"


def test_profiles_drift_rejected():
    profiles = load_profiles(ROOT / "qualification" / "resource_profiles.json")
    profiles["profiles"]["S"]["zip_parts"] = "many"
    with pytest.raises(ProfileError):
        validate_profiles(profiles)


def test_fail_closed_gate_rejects_over_limit_with_diagnostic(tmp_path):
    from scripts.resource_limits import make_text_node

    profiles = validate_profiles(load_profiles(ROOT / "qualification" / "resource_profiles.json"))
    s = {**profiles["profiles"]["S"], "id": "S"}
    over = make_text_node(s, over=True)
    try:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "partial.docx").write_bytes(b"partial")
        result = enforce_package(over, s, scratch=scratch)
        assert result["fail_closed"] is True
        codes = [d["code"] for d in result["diagnostics"]]
        assert "resource-limit-exceeded" in codes
        details = [d["details"] for d in result["diagnostics"]]
        assert any(d["profile"] == "S" and "actual" in d and "limit" in d for d in details)
        assert not scratch.exists()  # no partial output survives the gate
    finally:
        over.unlink(missing_ok=True)


def test_fail_closed_gate_passes_inside_limit(tmp_path):
    from scripts.resource_limits import make_text_node

    profiles = validate_profiles(load_profiles(ROOT / "qualification" / "resource_profiles.json"))
    s = {**profiles["profiles"]["S"], "id": "S"}
    inside = make_text_node(s, over=False)
    try:
        result = enforce_package(inside, s)
        assert result["fail_closed"] is False
        assert result["violations"] == []
    finally:
        inside.unlink(missing_ok=True)


def test_calibration_corruptions_fail_closed():
    profiles = validate_profiles(load_profiles(ROOT / "qualification" / "resource_profiles.json"))
    s = {**profiles["profiles"]["S"], "id": "S"}
    cal = ROOT / "corpus" / "calibration"
    for fixture in sorted(cal.glob("cal-*-over-S.docx")):
        result = enforce_package(fixture, s)
        assert result["fail_closed"] is True, fixture.name
    for fixture in sorted(cal.glob("cal-*-inside-S.docx")):
        result = enforce_package(fixture, s)
        assert result["fail_closed"] is False, fixture.name


# ---------------------------------------------------------------------------
# Office evidence gate (fail closed) + semantic retention rules
# ---------------------------------------------------------------------------


def _cell(consumer: str, phase: str, result: str, fixture: str = "plain.docx") -> dict:
    return {"consumer": consumer, "fixture": fixture, "phase": phase, "result": result, "reason": ""}


def test_blocking_gate_fails_closed_on_not_run_cells():
    consumers = [
        {"id": "word-windows-m365", "role": "blocking"},
        {"id": "word-macos-m365", "role": "blocking"},
    ]
    cells = [
        _cell("word-windows-m365", "open", "pass"),
        _cell("word-windows-m365", "repair-observation", "not-run"),
        _cell("word-macos-m365", "open", "not-run"),
    ]
    summary = summarize_blocking(cells, consumers)
    assert summary["gate"] == "fail"
    assert len(summary["blocking_not_pass_cells"]) == 2


def test_blocking_gate_passes_when_every_cell_passes():
    consumers = [{"id": "lo-windows", "role": "blocking-platform-integration"}]
    cells = [_cell("lo-windows", phase, "pass") for phase in ("open", "render", "save", "reopen", "retention")]
    summary = summarize_blocking(cells, consumers)
    assert summary["gate"] == "pass"


def _full_evidence() -> dict:
    """Synthetic evidence with an explicit pass verdict for every declared
    blocking consumer x phase x fixture cell; WPS (best-effort) is absent."""
    fixtures = [{"name": "fx-a.docx"}, {"name": "fx-b.docx"}]
    cells = []
    for consumer_id, spec in CONSUMERS.items():
        if spec.get("role") not in ("blocking", "blocking-platform-integration"):
            continue
        for fixture in fixtures:
            for phase in spec["phases"]:
                cells.append(
                    {"consumer": consumer_id, "fixture": fixture["name"], "phase": phase, "result": "pass", "reason": ""}
                )
    return {
        "schema": "docx2typed-office-evidence-1",
        "revision": 99,
        "generated": "2026-08-13T00:00:00Z",
        "consumers": [
            {"id": consumer_id, **spec, "available": False}
            for consumer_id, spec in CONSUMERS.items()
        ],
        "fixtures": fixtures,
        "cells": cells,
    }


def test_blocking_gate_fails_when_a_declared_blocking_cell_is_absent(capsys):
    """A declared blocking consumer with zero recorded cells (e.g.
    word-ltsc-2024 / word-macos-m365 / lo-fresh-linux / lo-still-linux on
    this runner) fails the gate with a listing — absence is never a pass."""
    evidence = _full_evidence()
    evidence["cells"] = [cell for cell in evidence["cells"] if cell["consumer"] != "lo-fresh-linux"]
    missing = missing_blocking_cells(evidence)
    assert any("lo-fresh-linux" in row for row in missing)
    assert all("wps" not in row for row in missing)  # best-effort stays non-blocking
    summary = verify(evidence)
    out = capsys.readouterr().out
    assert summary["gate"] == "fail"
    assert summary["missing_blocking_cells"] == missing
    assert "MISSING BLOCKING lo-fresh-linux" in out


def test_blocking_gate_fails_when_repair_observation_has_no_explicit_verdict(capsys):
    """Human repair-observation can only ever be not-run; without an
    explicit pass/fail verdict the declared row is missing and blocks."""
    evidence = _full_evidence()
    for cell in evidence["cells"]:
        if cell["phase"] == "repair-observation":
            cell["result"] = "not-run"
    missing = missing_blocking_cells(evidence)
    assert any("repair-observation" in row and "not-run" in row for row in missing)
    summary = verify(evidence)
    assert summary["gate"] == "fail"
    assert "repair-observation" in capsys.readouterr().out


def test_blocking_gate_passes_only_with_complete_blocking_matrix(capsys):
    """Every declared blocking consumer x phase x fixture verdict must be an
    explicit pass/fail; only then does the gate pass (WPS absent is fine)."""
    evidence = _full_evidence()
    assert missing_blocking_cells(evidence) == []
    summary = verify(evidence)
    assert summary["gate"] == "pass"
    assert summary.get("missing_blocking_cells", []) == []
    assert "gate: pass" in capsys.readouterr().out


def test_verify_rejects_evidence_that_mismatches_the_pinned_identity():
    """The evidence file is validated against the frozen identity: a
    substituted or drifted revision errors before any verdict is scored."""
    from scripts.protocol import semantic_sha256

    evidence = _full_evidence()
    with pytest.raises(ValueError, match="identity pin mismatch"):
        verify(evidence, expect_sha256="0" * 64)
    summary = verify(evidence, expect_sha256=semantic_sha256(evidence))
    assert summary["gate"] == "pass"


def test_retention_word_rule_is_strict_on_visible_text():
    before = semantic_signature(ROOT / "corpus/release/plain.docx")
    after = dict(before)
    after["visible_text_sha256"] = "0" * 64
    retention = evaluate_retention(before, after, "word-windows-m365")
    assert retention["retained"] is False
    assert any("visible_text" in diff for diff in retention["diffs"])


def test_retention_wps_rule_tolerates_navigation_bookmark_additions():
    before = semantic_signature(ROOT / "corpus/release/plain.docx")
    after = dict(before)
    after["bookmarks"] = list(before.get("bookmarks", [])) + ["_GoBack"]
    retention = evaluate_retention(before, after, "wps-windows")
    assert retention["retained"] is True


def test_retention_lo_rule_relaxes_revision_container_churn():
    before = semantic_signature(ROOT / "corpus/release/revisions.docx")
    after = dict(before)
    after["revision_marks"] = list(before["revision_marks"]) + ["ins:99"]
    retention = evaluate_retention(before, after, "lo-windows")
    assert retention["retained"] is True


def test_every_runnable_consumer_has_a_named_rewrite_rule():
    for consumer_id in ("word-windows-m365", "lo-windows", "wps-windows"):
        rule = CONSUMER_REWRITE_RULES.get(consumer_id)
        assert rule is not None, consumer_id
        assert rule["rule_id"].startswith(consumer_id.split("-")[0])
