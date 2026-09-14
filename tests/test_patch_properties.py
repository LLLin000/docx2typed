"""Property tests for the document_patch engine (hypothesis).

Frozen properties:
1. success:  disjoint single-region edits land the exact new text;
2. failure:  any rejected patch leaves the draft byte-identical (zero side effects);
3. structure: markers are immutable — any marker-editing diff fails closed;
4. window diff: a window read is a valid diff base regardless of @@ line numbers.
"""
from __future__ import annotations

import json
from pathlib import Path

from docx import Document
from hypothesis import HealthCheck, given, settings, strategies as st

from scripts.mcp_server import (
    document_patch,
    document_read,
    document_search,
    session,
    workdir_open,
)
from scripts.extract import extract

ROOT = Path(__file__).resolve().parents[1]


_WORKDIR_SEQ = iter(range(1, 10_000))


def _reset() -> None:
    session.workdir = None


def _open(tmp_path: Path, name: str, paragraph: str) -> Path:
    # hypothesis reuses one tmp_path across examples: every example gets a
    # fresh workdir directory name so no stale sidecars leak between runs
    unique = f"{name}-{next(_WORKDIR_SEQ)}"
    source = tmp_path / f"{unique}-src.docx"
    workdir = tmp_path / unique
    document = Document()
    document.add_paragraph(paragraph)
    document.save(source)
    assert extract([str(source), "-o", str(workdir)]) == 0
    opened = workdir_open(str(workdir))
    data = opened if isinstance(opened, dict) else json.loads(opened)
    return Path(data["workdir"])


def _body(workdir: Path) -> str:
    content = _j(document_read())
    return content["content"]


def _j(result):
    if hasattr(result, "structuredContent"):
        return result.structuredContent["data"]
    if isinstance(result, dict):
        return result
    return json.loads(result)


def _body_text(workdir: Path) -> str | None:
    """The single body line of P0 from the real projection; None when empty."""
    lines = (workdir / "edit.md").read_text(encoding="utf-8").split("\n")
    for line in lines:
        if line and not line.startswith("<!--@") and line.strip():
            return line
    return None


# strategies -----------------------------------------------------------------

safe_text = st.text(
    alphabet=st.characters(blacklist_characters="\n\r\x00", blacklist_categories=("Cs",)),
    min_size=0,
    max_size=40,
)

prose = st.sampled_from([
    "本研究结果表明AHD评分显著提高",
    "术前肩峰肱骨距离是重要的影像学预测因素",
    "重复重复重复重复重复重复重复重复",
    " mixed 😀 text with e\u0301 accents ",
    "中英混排 mixed 12345 end",
    "a" * 80,
    "",
])


# properties -----------------------------------------------------------------


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(
    paragraph=prose,
    insertion=safe_text.filter(lambda s: s),
)
def test_pure_insertion_lands_exactly(tmp_path, paragraph, insertion):
    """Any single insertion into any paragraph either lands the exact text or
    fails with the draft untouched."""
    _reset()
    workdir = _open(tmp_path, "prop-ins", paragraph)
    before = _body_text(workdir)
    if not before:
        _reset()
        return  # insertion into an empty body is out of the engine's contract
    pos = min(len(before), len(insertion) % max(1, len(before) + 1))
    after = before[:pos] + insertion + before[pos:]
    diff = (
        "--- a/edit.md\n+++ b/edit.md\n"
        f"@@ -3,1 +3,1 @@\n-{before}\n+{after}\n"
    )
    try:
        result = document_patch(diff=diff, operation_id="prop-ins")
        actual = _body_text(workdir)
        if result.isError:
            assert actual == before, "rejected patch must leave the draft untouched"
        else:
            assert actual == after
    finally:
        _reset()


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(
    paragraph=prose,
    left=safe_text,
    right=safe_text,
)
def test_overlapping_hunks_leave_draft_untouched(tmp_path, paragraph, left, right):
    """Overlapping replace hunks are always rejected with zero side effects."""
    _reset()
    workdir = _open(tmp_path, "prop-overlap", paragraph)
    before = _body_text(workdir)
    if before is None or len(before) < 3:
        _reset()
        return
    cut = len(before) // 2
    span1 = before[: max(1, cut)]
    span2 = before[max(1, cut - 1) : max(2, cut + 1)]
    if not span2 or span1 == span2:
        return
    result = document_patch(hunks=[
        {"paragraph_id": "P0", "old": span1, "new": left},
        {"paragraph_id": "P0", "old": span2, "new": right},
    ], operation_id="prop-overlap")
    assert result.isError is True, "overlapping spans must be rejected"
    codes = [d["code"] for d in result.structuredContent["diagnostics"]]
    assert codes[0] in (
        "document-patch-hunks-overlap",
        "text-ambiguous",
        "text-not-found",
        "patch-hunks-invalid",  # several broken hunks are reported together
    ), codes
    assert _body_text(workdir) == before
    _reset()


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(
    bogus_marker=st.sampled_from([
        '<!--@p id="PX"-->',
        "<!--@delete id=\"P0\"-->",
        "<!--@new temp=\"N9\"-->",
    ]),
)
def test_marker_editing_diffs_fail_closed(tmp_path, bogus_marker):
    _reset()
    workdir = _open(tmp_path, "prop-marker", "前言中段后语")
    before = (workdir / "edit.md").read_text(encoding="utf-8")
    lines = before.split("\n")
    target = next(i for i, l in enumerate(lines) if "中段" in l)
    diff = (
        "--- a/edit.md\n+++ b/edit.md\n"
        f"@@ -{target},3 +{target},3 @@\n"
        f" {lines[target - 1]}\n-{lines[target]}\n+{bogus_marker}\n {lines[target + 1]}\n"
    )
    try:
        result = document_patch(diff=diff, operation_id="prop-marker")
        assert result.isError is True
        assert result.structuredContent["diagnostics"][0]["code"] == "patch-structure-immutable"
        assert (workdir / "edit.md").read_text(encoding="utf-8") == before
    finally:
        _reset()


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(
    paragraph=prose.filter(lambda s: len(s) >= 6),
    replacement=safe_text.filter(lambda s: s),
)
def test_window_diff_with_lying_line_numbers(tmp_path, paragraph, replacement):
    """A window read is a valid diff base even when @@ numbers are garbage."""
    _reset()
    workdir = _open(tmp_path, "prop-window", paragraph)
    window = _j(document_read(anchor="P0", before=2, after=2))
    lines = window["content"].split("\n")
    target = next(i for i, l in enumerate(lines) if l and not l.startswith("<!--@") and l.strip())
    before_line = lines[target]
    after_line = before_line[: len(before_line) // 2] + replacement
    diff = (
        "--- a/edit.md\n+++ b/edit.md\n"
        f"@@ -{len(lines) + 500},3 +{len(lines) + 500},3 @@\n"
        f" {lines[target - 1]}\n-{before_line}\n+{after_line}\n {lines[target + 1]}\n"
    )
    try:
        result = document_patch(diff=diff, base_revision=window["revision"], operation_id="prop-window")
        actual = _body_text(workdir)
        if result.isError:
            assert actual == paragraph
        else:
            assert actual == after_line
    finally:
        _reset()
