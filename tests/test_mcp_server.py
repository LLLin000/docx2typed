"""docx2typed-mcp: span-free region-scoped editing tools and regions.md."""
from __future__ import annotations

import json
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from scripts.extract import extract
from scripts.mcp_server import (
    batch_edit,
    build_docx,
    commit_sync,
    delete_paragraph,
    diff_preview,
    get_paragraph,
    insert_paragraph,
    list_paragraphs,
    replace_text,
    revert,
    session,
    verify_output,
    workdir_open,
    workdir_status,
)


def _reset() -> None:
    session.workdir = None


def make_doc(path: Path) -> None:
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("前言")
    cn = paragraph.add_run("智能响应")
    cn.font.name = "宋体"
    cn._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    en = paragraph.add_run("ABC")
    en.font.name = "Times New Roman"
    paragraph.add_run("后语")
    document.add_paragraph("第二段")
    document.save(path)


def open_workdir(tmp_path: Path, name: str) -> Path:
    source = tmp_path / f"{name}-src.docx"
    workdir = tmp_path / name
    make_doc(source)
    assert extract([str(source), "-o", str(workdir)]) == 0
    return json.loads(workdir_open(str(workdir)))["workdir"]


def _j(result: str) -> dict:
    return json.loads(result)


def cn_en_runs(docx_path: Path) -> dict:
    return {r.text: r for r in Document(docx_path).paragraphs[0].runs}


def test_regions_md_generated_at_extract(tmp_path):
    _reset()
    workdir = open_workdir(tmp_path, "regions")
    regions = (Path(workdir) / "regions.md").read_text(encoding="utf-8")
    assert "## P0" in regions and "## P1" in regions
    assert "智能响应" in regions and "{s_" in regions
    assert "[token]" not in regions  # no tokens in this fixture


def test_get_paragraph_has_style_id_description_rpr(tmp_path):
    _reset()
    open_workdir(tmp_path, "styles")
    data = _j(get_paragraph("P0"))
    styles = data["styles"]
    cn_region = next(r for r in styles if r["text"] == "智能响应")
    en_region = next(r for r in styles if r["text"] == "ABC")
    assert cn_region["style_id"].startswith("s_") and cn_region["style_id"] != en_region["style_id"]
    assert cn_region["description"]  # non-empty label
    assert cn_region["rpr"].startswith("<w:rPr")  # full canonical XML present


def test_batch_edit_by_index_preserves_cn_en_fonts(tmp_path):
    _reset()
    open_workdir(tmp_path, "batch")
    result = _j(batch_edit(
        "P0",
        [
            {"region": 1, "new": "智能调控"},
            {"region": 2, "new": "XYZ"},
        ],
    ))
    assert result["edits_applied"] == 2 and result["state"] == "clean"
    output = json.loads(build_docx())["output"]
    assert _j(verify_output(output))["verified"] == output
    runs = cn_en_runs(output)
    assert runs["智能调控"]._element.rPr.rFonts.get(qn("w:eastAsia")) == "宋体"
    assert runs["XYZ"]._element.rPr.rFonts.get(qn("w:ascii")) == "Times New Roman"


def test_batch_edit_text_anchor_with_style_disambiguation(tmp_path):
    _reset()
    open_workdir(tmp_path, "anchor")
    _j(batch_edit("P0", [{"text": "ABC", "new": "XYZ"}]))
    assert _j(workdir_status())["state"] == "clean"
    data = _j(get_paragraph("P0"))
    assert "XYZ" in data["plain"] and "ABC" not in data["plain"]


def test_batch_edit_partial_old_inside_region(tmp_path):
    _reset()
    open_workdir(tmp_path, "partial")
    _j(batch_edit("P0", [{"region": 1, "old": "智能", "new": "智慧"}]))
    data = _j(get_paragraph("P0"))
    assert "智慧响应" in data["plain"]


def test_batch_edit_atomic_rejection(tmp_path):
    _reset()
    open_workdir(tmp_path, "atomic")
    try:
        batch_edit(
            "P0",
            [
                {"region": 1, "new": "智能调控"},
                {"region": 2, "old": "not-there", "new": "XYZ"},
            ],
        )
    except Exception as exc:
        assert "text-not-found" in str(exc)
    else:
        raise AssertionError("batch with one bad edit must fail")
    assert _j(workdir_status())["state"] == "clean"  # rolled back
    data = _j(get_paragraph("P0"))
    assert "智能响应" in data["plain"] and "智能调控" not in data["plain"]


def test_batch_edit_region_out_of_range_and_duplicate(tmp_path):
    _reset()
    open_workdir(tmp_path, "range")
    for bad, code in (
        ([{"region": 99, "new": "x"}], "region-out-of-range"),
        ([{"region": 0, "new": "a"}, {"region": 0, "new": "b"}], "invalid-edit"),
    ):
        try:
            batch_edit("P0", bad)
        except Exception as exc:
            assert code in str(exc)
        else:
            raise AssertionError(f"expected {code}")


def test_regions_md_auto_updates_after_edit(tmp_path):
    _reset()
    workdir = open_workdir(tmp_path, "autoupdate")
    _j(batch_edit("P0", [{"region": 1, "new": "新词"}]))
    regions = (Path(workdir) / "regions.md").read_text(encoding="utf-8")
    assert "新词" in regions and "智能响应" not in regions


def test_replace_text_still_rejects_cross_region(tmp_path):
    _reset()
    open_workdir(tmp_path, "cross")
    try:
        replace_text("P0", "智能响应ABC", "新词XYZ")
    except Exception as exc:
        assert "cross-region-text" in str(exc)
    else:
        raise AssertionError("cross-region replacement must be rejected")


def test_full_workflow_through_mcp(tmp_path):
    _reset()
    open_workdir(tmp_path, "flow")
    _j(insert_paragraph("P0", "新增段"))
    _j(delete_paragraph("P1"))
    _j(commit_sync())
    _j(revert())
    output = json.loads(build_docx())["output"]
    assert _j(verify_output(output))["verified"] == output
    texts = [p.text for p in Document(output).paragraphs]
    assert "新增段" in texts and "第二段" not in texts
    assert list_paragraphs()
