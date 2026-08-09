"""docx2typed-mcp: span-free region-scoped editing tools and regions.md."""
from __future__ import annotations

import json
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from scripts.extract import extract
from scripts.review_queue import dispatch, upsert_event
from scripts.review_collab import stage_patch
from scripts.mcp_server import (
    _fnv1a_utf16,
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
    review_apply_patch,
    review_preflight,
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



def test_review_apply_patch_settles_human_text_before_agent_write(tmp_path):
    _reset()
    workdir = Path(open_workdir(tmp_path, "human-patch"))
    current = json.loads(review_preflight())["current_snapshot"]["id"]
    paragraph = json.loads(get_paragraph("P0"))
    paragraph_text = paragraph["plain"]
    before = "智能响应"
    start = paragraph_text.index(before)
    target = {
        "start_offset": start,
        "end_offset": start + len(before),
        "expected_text": before,
        "left_context": paragraph_text[max(0, start - 100):start],
        "right_context": paragraph_text[start + len(before):start + len(before) + 100],
        "paragraph_fingerprint": _fnv1a_utf16(paragraph_text),
        "region_fingerprint": _fnv1a_utf16(before),
        "style_region_ids": list(dict.fromkeys(region["style_id"] for region in paragraph["styles"])),
    }
    event = upsert_event(
        workdir,
        {
            "type": "patch",
            "client_id": "human:patch-1",
            "origin": "human_ui",
            "author": "Lin",
            "parent_snapshot": current,
            "paragraph_id": "P0",
            "kind": "replace",
            "target": target,
            "before": before,
            "after": "智能调控",
        },
    )
    queued = dispatch(workdir)
    assert queued[0]["event_id"] == event["event_id"]

    result = json.loads(review_apply_patch(event["event_id"]))
    assert result["state"] == "applied"
    assert json.loads(review_apply_patch(event["event_id"]))["state"] == "already-applied"
    assert result["commit"]["current_snapshot"]["id"] == "C1"
    assert "智能调控" in json.loads(get_paragraph("P0"))["plain"]
    assert json.loads(review_preflight())["ready"] is True

def test_review_apply_patch_commits_staged_batch_atomically(tmp_path):
    _reset()
    workdir = Path(open_workdir(tmp_path, "human-batch"))
    current = json.loads(review_preflight())["current_snapshot"]["id"]
    paragraph = json.loads(get_paragraph("P0"))
    paragraph_text = paragraph["plain"]
    style_region_ids = list(dict.fromkeys(region["style_id"] for region in paragraph["styles"]))

    def make_patch(before: str, after: str, parent: str, client_id: str) -> dict[str, object]:
        start = paragraph_text.index(before)
        return {
            "type": "patch",
            "client_id": client_id,
            "origin": "human_ui",
            "author": "Lin",
            "parent_snapshot": parent,
            "paragraph_id": "P0",
            "kind": "replace",
            "target": {
                "start_offset": start,
                "end_offset": start + len(before),
                "expected_text": before,
                "left_context": paragraph_text[max(0, start - 100):start],
                "right_context": paragraph_text[start + len(before):start + len(before) + 100],
                "paragraph_fingerprint": _fnv1a_utf16(paragraph_text),
                "region_fingerprint": _fnv1a_utf16(before),
                "style_region_ids": style_region_ids,
            },
            "before": before,
            "after": after,
        }

    first = stage_patch(workdir, make_patch("前言", "导言", current, "batch:first"))
    second = stage_patch(workdir, make_patch("智能响应", "智能调控", first["staged_snapshot"], "batch:second"))
    dispatch(workdir)

    result = json.loads(review_apply_patch(first["event_id"]))
    assert result["state"] == "applied"
    assert result["commit"]["current_snapshot"]["id"] == "C1"
    assert {event["delivery_state"] for event in result["events"]} == {"applied"}
    assert "导言智能调控" in json.loads(get_paragraph("P0"))["plain"]

def test_track_mode_dirty_draft_sequential_edits(tmp_path):
    """Regression: consecutive replace_text on a dirty draft must keep the
    session's track mode (pending revisions + trackChanges off would
    otherwise re-infer ambiguous and reject every later edit)."""
    import re as _re
    import zipfile

    _reset()
    source = tmp_path / "track-src.docx"
    workdir = tmp_path / "track"
    make_doc(source)
    # inject a pending revision into the SOURCE, with trackChanges off
    with zipfile.ZipFile(source) as z:
        files = {n: z.read(n) for n in z.namelist()}
    doc = files["word/document.xml"]
    ins = (
        '<w:ins w:id="99" w:author="tester" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:t>修订词</w:t></w:r></w:ins>'
    ).encode()
    doc = doc.replace(
        "<w:r><w:t>前言</w:t></w:r>".encode(),
        "<w:r><w:t>前</w:t></w:r>".encode() + ins + "<w:r><w:t>言</w:t></w:r>".encode(),
        1,
    )
    files["word/document.xml"] = doc
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    assert extract([str(source), "-o", str(workdir)]) == 0
    _j(workdir_open(str(workdir), track=True, author="AI润色"))
    # consecutive edits across paragraphs on the dirty draft, then one
    # preview + one commit (the draft -> preview -> commit model)
    _j(replace_text("P0", "智能响应", "智能调控"))
    _j(replace_text("P1", "第二段", "第二段落"))
    _j(replace_text("P0", "后语", "后文"))
    preview = _j(diff_preview())
    assert preview["state"] == "dirty"
    assert len(preview["hunks"]) >= 3
    committed = _j(commit_sync())
    assert committed["state"] == "clean"
    assert committed["edit_mode"] == "track"
    output = json.loads(build_docx())["output"]
    with zipfile.ZipFile(output) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert xml.count("<w:ins") >= 3 + 1  # 3 new + the pre-existing one
    assert _re.search(r'w:author="AI润色"', xml)
