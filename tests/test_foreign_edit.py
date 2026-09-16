"""Receipt-bound foreign candidate admission with a deterministic fake mutator."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from docx import Document

from scripts import main
from scripts.mcp_server import (
    _commit_decision,
    build_docx,
    commit_sync,
    foreign_edit_adopt,
    foreign_edit_prepare,
    replace_text,
    session,
    workspace_identity_fields,
    workdir_open,
)
from scripts.store import head_version, history_list


def _data(result) -> dict:
    payload = result.structuredContent
    return payload.get("data", {}) if isinstance(payload, dict) else {}


def _failure(result) -> str | None:
    if not getattr(result, "isError", False):
        return None
    payload = result.structuredContent or {}
    return (payload.get("diagnostics") or [{}])[0].get("code")


def _make_docx(path: Path) -> None:
    document = Document()
    document.add_paragraph("Initial paragraph one")
    document.add_paragraph("Base paragraph two")
    document.save(str(path))


def _open(tmp_path: Path, name: str) -> Path:
    source = tmp_path / f"{name}.docx"
    _make_docx(source)
    workdir = tmp_path / name
    assert main(["--json", "extract", str(source), "-o", str(workdir), "--operation-id", f"{name}-extract"]) == 0
    session.workdir = None
    session.last_build_output = None
    assert _failure(workdir_open(str(workdir), track=False)) is None
    assert _failure(replace_text("P0", "Initial paragraph one", "Base paragraph one", operation_id=f"{name}-edit")) is None
    saved = commit_sync(operation_id=f"{name}-save")
    assert _failure(saved) is None, saved
    assert _data(saved).get("version", {}).get("created"), json.dumps(_data(saved))
    assert not _commit_decision(workdir)["version_dirty"], json.dumps(_commit_decision(workdir))
    return workdir


def _mutate_text(candidate: Path, old: str, new: str) -> None:
    temporary = candidate.with_suffix(".mutated.docx")
    with zipfile.ZipFile(candidate, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                assert old.encode("utf-8") in payload
                payload = payload.replace(old.encode("utf-8"), new.encode("utf-8"), 1)
            target.writestr(info, payload)
    temporary.replace(candidate)


def _mutate_format(candidate: Path) -> None:
    temporary = candidate.with_suffix(".format.docx")
    with zipfile.ZipFile(candidate, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                marker = b"<w:r>"
                assert marker in payload
                payload = payload.replace(
                    marker,
                    b"<w:r><w:rPr><w:b/></w:rPr>",
                    1,
                )
            target.writestr(info, payload)
    temporary.replace(candidate)



def _mutate_style_definition(candidate: Path) -> None:
    temporary = candidate.with_suffix(".style.docx")
    with zipfile.ZipFile(candidate, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/styles.xml":
                marker = b'w:val="Normal"'
                assert marker in payload
                payload = payload.replace(marker, b'w:val="Normal foreign"', 1)
            target.writestr(info, payload)
    temporary.replace(candidate)


def _mutate_normalization(candidate: Path) -> None:
    temporary = candidate.with_suffix(".normalization.docx")
    with zipfile.ZipFile(candidate, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                marker = b"<w:document "
                assert marker in payload
                payload = payload.replace(
                    marker,
                    b'<w:document w:rsidR="00000000" ',
                    1,
                )
            target.writestr(info, payload)
    temporary.replace(candidate)


def _mutate_unattributed_document(candidate: Path) -> None:
    temporary = candidate.with_suffix(".unattributed.docx")
    with zipfile.ZipFile(candidate, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                marker = b"<w:document "
                assert marker in payload
                payload = payload.replace(
                    marker,
                    b'<w:document w:customAttr="foreign" ',
                    1,
                )
            target.writestr(info, payload)
    temporary.replace(candidate)

def _add_opaque_part(candidate: Path) -> None:
    temporary = candidate.with_suffix(".opaque.docx")
    with zipfile.ZipFile(candidate, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.writestr("customXml/item2.xml", b"<opaque/>\n")
    temporary.replace(candidate)


def _mutate_section(candidate: Path) -> None:
    temporary = candidate.with_suffix(".section.docx")
    with zipfile.ZipFile(candidate, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                assert b'<w:pgSz w:w="12240" w:h="15840"/>' in payload
                payload = payload.replace(
                    b'<w:pgSz w:w="12240" w:h="15840"/>',
                    b'<w:pgSz w:w="15840" w:h="12240" w:orient="landscape"/>',
                    1,
                )
            target.writestr(info, payload)
    temporary.replace(candidate)


def _append_paragraph(candidate: Path, text: str) -> None:
    """Append one paragraph the way an external editor saves: a new w:p plus
    whatever package churn that tool brings."""
    document = Document(str(candidate))
    document.add_paragraph(text)
    document.save(str(candidate))


def test_prepare_rejects_live_workdir_output(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "prepare-isolation")
    result = foreign_edit_prepare(
        output=str(workdir / "candidate.docx"),
        operation_id="prepare-isolation",
    )
    assert _failure(result) == "foreign-candidate-invalid"
    assert not (workdir / "candidate.docx").exists()


def test_prepare_fake_mutator_adopts_next_version_same_workspace(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "prepared")
    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-1")
    assert _failure(prepared) is None, prepared
    details = _data(prepared)
    candidate = Path(details["candidate"])
    receipt = Path(details["receipt"])
    assert candidate.is_file()
    assert receipt.is_file()
    receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_data["schema"] == "docx2typed-foreign-candidate-1"
    assert receipt_data["base_version"] == "V1"

    _mutate_text(candidate, "Base paragraph one", "Foreign paragraph one")
    adopted = foreign_edit_adopt(str(candidate), operation_id="adopt-1")
    assert _failure(adopted) is None, json.dumps(adopted.structuredContent, ensure_ascii=False)
    result = _data(adopted)
    assert result["base_version"] == "V1"
    assert result["decision"] == "auto"
    assert head_version(workdir)["version"] == "V2"
    assert workspace_identity_fields(workdir)["family_id"] == receipt_data["family_id"]
    assert workspace_identity_fields(workdir)["workspace_id"] == receipt_data["workspace_id"]
    version = next(item for item in history_list(workdir)["versions"] if item["version"] == "V2")
    assert version["origin"] == "baseline-transition"
    assert version["metadata"]["foreign_edit"]["candidate_id"] == receipt_data["candidate_id"]
    assert "Foreign paragraph one" in (workdir / "typed.md").read_text(encoding="utf-8")


def test_out_of_target_and_opaque_changes_refuse_without_moving_head(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "gates")
    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-gates")
    candidate = Path(_data(prepared)["candidate"])
    _mutate_text(candidate, "Base paragraph one", "Foreign paragraph one")
    _mutate_text(candidate, "Base paragraph two", "Unexpected paragraph two")
    refused = foreign_edit_adopt(str(candidate), operation_id="adopt-outside")
    assert _failure(refused) == "foreign-out-of-scope"
    assert head_version(workdir)["version"] == "V1"

    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-opaque")
    opaque_candidate = Path(_data(prepared)["candidate"])
    _add_opaque_part(opaque_candidate)
    refused = foreign_edit_adopt(str(opaque_candidate), operation_id="adopt-opaque")
    assert _failure(refused) in {"foreign-opaque-or-package-changed", "foreign-package-invalid"}
    assert head_version(workdir)["version"] == "V1"


def test_head_drift_and_missing_receipt_require_explicit_lineage(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "lineage")
    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-drift")
    assert _failure(prepared) is None
    candidate = Path(_data(prepared)["candidate"])
    _mutate_text(candidate, "Base paragraph one", "Foreign paragraph one")
    assert _failure(replace_text("P1", "Base paragraph two", "Native paragraph two", operation_id="native-1")) is None
    assert _failure(commit_sync(operation_id="native-save")) is None
    assert _failure(foreign_edit_adopt(str(candidate), operation_id="adopt-drift")) == "foreign-edit-conflict"
    assert head_version(workdir)["version"] == "V2"

    assert _failure(workdir_open(str(workdir), track=False)) is None
    # The candidate is deliberately detached from the receipt path.
    manual = tmp_path / "manual-candidate.docx"
    with zipfile.ZipFile(candidate, "r") as source_zip, zipfile.ZipFile(manual, "w") as target_zip:
        for info in source_zip.infolist():
            target_zip.writestr(info, source_zip.read(info.filename))
    manual_result = foreign_edit_adopt(str(manual), operation_id="manual-no-lineage")
    assert _failure(manual_result) == "foreign-lineage-required"


def test_manual_candidate_needs_explicit_family_and_base(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "manual")
    manual = tmp_path / "manual-candidate.docx"
    built = build_docx(output=str(manual), operation_id="manual-build")
    assert _failure(built) is None, built
    _mutate_text(manual, "Base paragraph one", "Manual paragraph one")
    identity = workspace_identity_fields(workdir)
    adopted = foreign_edit_adopt(
        str(manual),
        family_id=str(identity["family_id"]),
        base_version="V1",
        target=["paragraph:P0"],
        operation_id="manual-adopt",
    )
    assert _failure(adopted) is None, adopted
    assert head_version(workdir)["version"] == "V2"
    assert Path(str(manual) + ".foreign-candidate.json").is_file()


def test_format_change_is_attributed_inside_target(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "format")
    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-format")
    assert _failure(prepared) is None, prepared
    candidate = Path(_data(prepared)["candidate"])
    _mutate_format(candidate)
    adopted = foreign_edit_adopt(str(candidate), operation_id="adopt-format")
    assert _failure(adopted) is None, adopted
    changes = _data(adopted)["changes"]
    assert any(item["kind"] == "format" and item["paragraph_id"] == "P0" for item in changes)
    assert head_version(workdir)["version"] == "V2"


def test_known_structured_xml_normalization_is_recorded(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "normalization")
    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-normalization")
    assert _failure(prepared) is None, prepared
    candidate = Path(_data(prepared)["candidate"])
    _mutate_normalization(candidate)
    adopted = foreign_edit_adopt(str(candidate), operation_id="adopt-normalization")
    assert _failure(adopted) is None, adopted
    result = _data(adopted)
    assert result["normalization"] == ["word/document.xml"]
    assert result["changes"] == []
    assert head_version(workdir)["version"] == "V2"


def test_style_dependency_outside_paragraph_target_is_refused(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "style-scope")
    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-style-scope")
    assert _failure(prepared) is None, prepared
    candidate = Path(_data(prepared)["candidate"])
    _mutate_style_definition(candidate)
    refused = foreign_edit_adopt(str(candidate), operation_id="adopt-style-scope")
    assert _failure(refused) in {"foreign-out-of-scope", "foreign-opaque-or-package-changed"}
    assert head_version(workdir)["version"] == "V1"


def test_explainable_style_dependency_requires_one_consent(tmp_path: Path, monkeypatch) -> None:
    workdir = _open(tmp_path, "style-consent")
    prepared = foreign_edit_prepare(target=["style:Normal"], operation_id="prepare-style-consent")
    assert _failure(prepared) is None, prepared
    candidate = Path(_data(prepared)["candidate"])
    _mutate_style_definition(candidate)
    first = foreign_edit_adopt(str(candidate), operation_id="adopt-style-consent")
    assert _failure(first) == "foreign-consent-required"
    details = (first.structuredContent.get("diagnostics") or [{}])[0].get("details") or {}
    token = details.get("consent_token")
    assert isinstance(token, str) and token
    monkeypatch.setattr("scripts.foreign_edit.now_iso", lambda: "2099-01-01T00:00:00+00:00")
    second = foreign_edit_adopt(
        str(candidate),
        consent_token=token,
        operation_id="adopt-style-consent-confirmed",
    )
    assert _failure(second) is None, second
    assert _data(second)["decision"] == "consent"
    assert any(item["kind"] == "dependency" for item in _data(second)["changes"])
    assert head_version(workdir)["version"] == "V2"


def test_manual_consent_token_survives_retry_without_receipt(tmp_path: Path, monkeypatch) -> None:
    workdir = _open(tmp_path, "manual-consent")
    manual = tmp_path / "manual-consent-candidate.docx"
    built = build_docx(output=str(manual), operation_id="manual-consent-build")
    assert _failure(built) is None, built
    _mutate_style_definition(manual)
    identity = workspace_identity_fields(workdir)
    kwargs = {
        "family_id": str(identity["family_id"]),
        "base_version": "V1",
        "target": ["style:Normal"],
    }
    first = foreign_edit_adopt(
        str(manual), operation_id="manual-consent-question", **kwargs
    )
    assert _failure(first) == "foreign-consent-required"
    details = (first.structuredContent.get("diagnostics") or [{}])[0].get("details") or {}
    token = details.get("consent_token")
    assert isinstance(token, str) and token
    monkeypatch.setattr("scripts.foreign_edit.now_iso", lambda: "2099-01-01T00:00:00+00:00")
    second = foreign_edit_adopt(
        str(manual),
        consent_token=token,
        operation_id="manual-consent-confirmed",
        **kwargs,
    )
    assert _failure(second) is None, second
    assert head_version(workdir)["version"] == "V2"


def test_page_setup_change_is_attributed_only_inside_a_section_target(tmp_path: Path) -> None:
    outside = _open(tmp_path, "section-outside")
    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-section-outside")
    assert _failure(prepared) is None, prepared
    candidate = Path(_data(prepared)["candidate"])
    _mutate_section(candidate)
    refused = foreign_edit_adopt(str(candidate), operation_id="adopt-section-outside")
    assert _failure(refused) == "foreign-out-of-scope", refused
    assert head_version(outside)["version"] == "V1"

    workdir = _open(tmp_path, "section-inside")
    prepared = foreign_edit_prepare(target=["section:0"], operation_id="prepare-section")
    assert _failure(prepared) is None, prepared
    candidate = Path(_data(prepared)["candidate"])
    _mutate_section(candidate)
    adopted = foreign_edit_adopt(str(candidate), operation_id="adopt-section")
    assert _failure(adopted) is None, json.dumps(adopted.structuredContent, ensure_ascii=False)
    result = _data(adopted)
    assert result["decision"] == "auto"
    assert [item["kind"] for item in result["changes"]] == ["section"]
    assert result["changes"][0]["section"] == 0
    assert head_version(workdir)["version"] == "V2"


def test_unattributed_structured_package_change_is_refused(tmp_path: Path) -> None:
    workdir = _open(tmp_path, "unattributed")
    prepared = foreign_edit_prepare(target=["paragraph:P0"], operation_id="prepare-unattributed")
    assert _failure(prepared) is None, prepared
    candidate = Path(_data(prepared)["candidate"])
    _mutate_unattributed_document(candidate)
    refused = foreign_edit_adopt(str(candidate), operation_id="adopt-unattributed")
    assert _failure(refused) == "foreign-opaque-or-package-changed"
    assert head_version(workdir)["version"] == "V1"


def test_hand_edited_export_returns_as_a_version_in_its_own_timeline(tmp_path: Path) -> None:
    """Issue #95: a hand-edited export re-enters its own family as the next
    Version (a baseline transition), not as a new workspace. An omitted target
    is the whole-document manual re-entry route, and a real hand edit changes
    the paragraph count."""
    workdir = _open(tmp_path, "whole-document")
    manual = tmp_path / "hand-edited.docx"
    built = build_docx(output=str(manual), operation_id="whole-document-build")
    assert _failure(built) is None, built
    _mutate_text(manual, "Base paragraph two", "Hand written paragraph two")
    _append_paragraph(manual, "Hand written addition")

    identity = workspace_identity_fields(workdir)
    adopted = foreign_edit_adopt(
        str(manual),
        family_id=str(identity["family_id"]),
        base_version="V1",
        operation_id="whole-document-adopt",
    )
    assert _failure(adopted) is None, json.dumps(adopted.structuredContent, ensure_ascii=False)
    result = _data(adopted)
    assert result["base_version"] == "V1"
    assert result["decision"] == "auto"
    assert head_version(workdir)["version"] == "V2"

    changes = result["changes"]
    assert any(
        item["kind"] == "semantic" and item["paragraph_id"] == "P1" for item in changes
    ), changes
    assert any(item.get("state") == "added" for item in changes), changes
    typed = (workdir / "typed.md").read_text(encoding="utf-8")
    assert "Hand written paragraph two" in typed
    assert "Hand written addition" in typed

    # same family, same timeline: the transition re-roots the baseline only
    after = workspace_identity_fields(workdir)
    assert after["family_id"] == identity["family_id"]
    assert after["workspace_id"] == identity["workspace_id"]
    versions = history_list(workdir)["versions"]
    version = next(item for item in versions if item["version"] == "V2")
    assert version["origin"] == "baseline-transition"
    assert "V1" in [item["version"] for item in versions]
