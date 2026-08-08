"""Generate the deterministic release-corpus fixtures (corpus/release/*.docx).

Every fixture is synthetic, committed, and byte-stable across runs so the
release task suite is reproducible on any machine (no dependence on the
gitignored real corpus). Paragraph ids are deterministic by construction;
task definitions in capabilities/tasks/*.json reference them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

REPO_ROOT = Path(__file__).resolve().parent.parent


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
V_NS = "urn:schemas-microsoft-com:vml"
FOOTNOTES_REL = f"{R_NS}/footnotes"
ENDNOTES_REL = f"{R_NS}/endnotes"
FOOTNOTES_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
ENDNOTES_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"


def _set_east_asia(run, font: str) -> None:
    run.font.name = font
    rpr = run._r.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), font)


def _plain(output: Path) -> None:
    document = Document()
    document.add_paragraph("本发明涉及生物医用材料技术领域。剂量为 20 mg。")  # P0
    document.add_paragraph("实施例1采用 250 mg 剂量。")  # P1 (avoid "25 mg" collision)
    document.add_paragraph("The quick brown fox.")  # P2
    document.add_paragraph("结束段落。")  # P3
    document.add_paragraph("ABC denotes the control group.")  # P4
    document.add_paragraph("重复句子内容 重复句子内容。")  # P5 (ambiguous target)
    document.save(output)


def _styled(output: Path) -> None:
    document = Document()
    p0 = document.add_paragraph()
    p0.add_run("前缀")
    r = p0.add_run("中文区域内容")
    _set_east_asia(r, "宋体")
    p0.add_run("后缀")
    p1 = document.add_paragraph()
    p1.add_run("Lead ")
    r = p1.add_run("EnglishRegion")
    r.font.name = "Times New Roman"
    p1.add_run(" tail")
    p2 = document.add_paragraph()  # two adjacent regions: cross-region target
    r = p2.add_run("第一区域")
    _set_east_asia(r, "宋体")
    r = p2.add_run("第二区域")
    r.font.name = "Times New Roman"
    p3 = document.add_paragraph()  # leading/trailing formatting spaces
    p3.add_run(" 前导空格文字  ")
    p4 = document.add_paragraph()  # CJK + English regions for batch edit
    r = p4.add_run("智能响应")
    _set_east_asia(r, "宋体")
    p4.add_run("材料与 ")
    r = p4.add_run("ABC")
    r.font.name = "Times New Roman"
    p4.add_run(" 对照组相比。")
    document.save(output)


def _add_hyperlink(document: Document, paragraph, text: str, target: str) -> str:
    relationship_id = document.part.relate_to(target, R_NS + "/hyperlink", is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    run = OxmlElement("w:r")
    text_el = OxmlElement("w:t")
    text_el.text = text
    run.append(text_el)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)
    return relationship_id


def _anchors(output: Path) -> None:
    document = Document()
    p0 = document.add_paragraph()
    p0.add_run("超链接：")
    _add_hyperlink(document, p0, "点击此处", "https://example.com/docx2typed")
    p1 = document.add_paragraph()
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), "0")
    start.set(qn("w:name"), "bm1")
    p1._p.append(start)
    p1.add_run("关键内容")
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), "0")
    p1._p.append(end)
    comment_run = p1.add_run("批注区域")
    document.add_comment(comment_run, text="锚点附近批注", author="tester")
    document.save(output)


def _table(output: Path) -> None:
    document = Document()
    document.add_paragraph("表前")
    t0 = document.add_table(rows=3, cols=3)
    for row, values in enumerate([["A", "B", "C"], ["a1", "PVA", "a3"], ["b1", "b2", "b3"]]):
        for col, value in enumerate(values):
            t0.cell(row, col).text = value
    nested = t0.cell(1, 1).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "内层"
    t1 = document.add_table(rows=3, cols=3)
    for row, values in enumerate([["X", "Y", "Z"], ["x1", "x2", "x3"], ["y1", "PVA", "y3"]]):
        for col, value in enumerate(values):
            t1.cell(row, col).text = value
    document.add_paragraph("表后")
    document.save(output)


def _boxes(output: Path) -> None:
    from lxml import etree

    document = Document()
    document.add_paragraph("正文前")
    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    pict = etree.SubElement(run._r, f"{{{W_NS}}}pict", nsmap={"v": V_NS})
    shape = etree.SubElement(pict, f"{{{V_NS}}}shape")
    shape.set("style", "width:100pt;height:50pt")
    textbox = etree.SubElement(shape, f"{{{V_NS}}}textbox")
    txbx = etree.SubElement(textbox, f"{{{W_NS}}}txbxContent")
    tp = etree.SubElement(txbx, f"{{{W_NS}}}p")
    tr = etree.SubElement(tp, f"{{{W_NS}}}r")
    tt = etree.SubElement(tr, f"{{{W_NS}}}t")
    tt.text = "框内文字"
    document.add_paragraph("正文后")
    document.save(output)


def _add_reference_part(
    files: dict[str, bytes],
    *,
    marker: str,
    reference_tag: str,
    part_name: str,
    relationship_type: str,
    content_type: str,
    content: str,
) -> None:
    document_xml = files["word/document.xml"].decode("utf-8")
    text_position = document_xml.find(f">{marker}</w:t>")
    if text_position < 0:
        raise ValueError(f"missing marker: {marker}")
    paragraph_end = document_xml.find("</w:p>", text_position)
    reference = f'<w:r><w:{reference_tag} w:id="1"/></w:r>'
    document_xml = document_xml[:paragraph_end] + reference + document_xml[paragraph_end:]
    files["word/document.xml"] = document_xml.encode("utf-8")
    files[part_name] = content.encode("utf-8")
    relationships = files["word/_rels/document.xml.rels"].decode("utf-8")
    ids = [int(value) for value in re.findall(r'Id="rId(\d+)"', relationships)]
    relationship_id = f"rId{max(ids, default=0) + 1}"
    relationship = (
        f'<Relationship Id="{relationship_id}" Type="{relationship_type}" '
        f'Target="{part_name.removeprefix("word/")}"/>'
    )
    relationships = relationships.replace("</Relationships>", relationship + "</Relationships>")
    files["word/_rels/document.xml.rels"] = relationships.encode("utf-8")
    content_types = files["[Content_Types].xml"].decode("utf-8")
    override = f'<Override PartName="/{part_name}" ContentType="{content_type}"/>'
    content_types = content_types.replace("</Types>", override + "</Types>")
    files["[Content_Types].xml"] = content_types.encode("utf-8")


def _parts(output: Path) -> None:
    document = Document()
    section = document.sections[0]
    section.header.paragraphs[0].text = "Draft v1"
    section.footer.paragraphs[0].text = "Page"
    document.add_paragraph("正文段落")
    document.add_paragraph("FOOTNOTE-SLOT")
    document.add_paragraph("ENDNOTE-SLOT")
    document.save(output)
    with zipfile.ZipFile(output) as archive:
        files = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    _add_reference_part(
        files,
        marker="FOOTNOTE-SLOT",
        reference_tag="footnoteReference",
        part_name="word/footnotes.xml",
        relationship_type=FOOTNOTES_REL,
        content_type=FOOTNOTES_CONTENT_TYPE,
        content=(
            f'<w:footnotes xmlns:w="{W_NS}">'
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/>'
            "</w:r></w:p></w:footnote>"
            '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r>'
            "<w:continuationSeparator/></w:r></w:p></w:footnote>"
            '<w:footnote w:id="1"><w:p><w:r><w:t>脚注内容</w:t>'
            "</w:r></w:p></w:footnote></w:footnotes>"
        ),
    )
    _add_reference_part(
        files,
        marker="ENDNOTE-SLOT",
        reference_tag="endnoteReference",
        part_name="word/endnotes.xml",
        relationship_type=ENDNOTES_REL,
        content_type=ENDNOTES_CONTENT_TYPE,
        content=(
            f'<w:endnotes xmlns:w="{W_NS}">'
            '<w:endnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/>'
            "</w:r></w:p></w:endnote>"
            '<w:endnote w:type="continuationSeparator" w:id="0"><w:p><w:r>'
            "<w:continuationSeparator/></w:r></w:p></w:endnote>"
            '<w:endnote w:id="1"><w:p><w:r><w:t>尾注内容</w:t>'
            "</w:r></w:p></w:endnote></w:endnotes>"
        ),
    )
    with tempfile.NamedTemporaryFile(prefix="parts-fixture-", suffix=".docx", dir=output.parent, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        os.replace(temp_path, output)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _revision_run(text: str, *, deleted: bool = False) -> ET.Element:
    run = ET.Element(f"{{{W_NS}}}r")
    t = ET.SubElement(run, f"{{{W_NS}}}{'delText' if deleted else 't'}")
    t.text = text
    return run


def _revision(document: Document, paragraph, tag: str, w_id: str, text: str, *, deleted: bool = False) -> None:
    rev = OxmlElement(tag)
    rev.set(qn("w:id"), w_id)
    rev.set(qn("w:author"), "审稿人")
    rev.set(qn("w:date"), "2026-08-06T10:12:00Z")
    run = OxmlElement("w:r")
    t = OxmlElement("w:delText" if deleted else "w:t")
    t.text = text
    run.append(t)
    rev.append(run)
    paragraph._p.append(rev)


def _revisions(output: Path) -> None:
    document = Document()
    p0 = document.add_paragraph("修订前文")
    _revision(document, p0, "w:ins", "1", "已插入内容")  # 1
    p0.add_run("修订后文")
    p1 = document.add_paragraph("保留")
    _revision(document, p1, "w:del", "2", "旧文本", deleted=True)  # 2
    # paragraph-mark revision: self-closing ins inside pPr>rPr (standard shape)
    p2 = document.add_paragraph("段落标记段")
    ppr = p2._p.get_or_add_pPr()
    mark_rpr = OxmlElement("w:rPr")
    mark = OxmlElement("w:ins")
    mark.set(qn("w:id"), "3")
    mark.set(qn("w:author"), "审稿人")
    mark.set(qn("w:date"), "2026-08-06T10:12:00Z")
    mark_rpr.append(mark)
    ppr.append(mark_rpr)
    # field containing an insertion revision (opaque interior)
    p3 = document.add_paragraph()
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), ' PAGE ')
    fld_ins = OxmlElement("w:ins")
    fld_ins.set(qn("w:id"), "4")
    fld_ins.set(qn("w:author"), "审稿人")
    fld_ins.set(qn("w:date"), "2026-08-06T10:12:00Z")
    fld_run = OxmlElement("w:r")
    fld_t = OxmlElement("w:t")
    fld_t.text = "字段内插入"
    fld_run.append(fld_t)
    fld_ins.append(fld_run)
    fld.append(fld_ins)
    p3._p.append(fld)
    # math containing an insertion revision (opaque interior)
    p4 = document.add_paragraph()
    math = OxmlElement("m:oMath")
    math_ins = OxmlElement("w:ins")
    math_ins.set(qn("w:id"), "5")
    math_ins.set(qn("w:author"), "审稿人")
    math_ins.set(qn("w:date"), "2026-08-06T10:12:00Z")
    m_run = OxmlElement("w:r")
    m_t = OxmlElement("w:t")
    m_t.text = "公式内插入"
    m_run.append(m_t)
    math_ins.append(m_run)
    math.append(math_ins)
    p4._p.append(math)
    p5 = document.add_paragraph()
    _revision(document, p5, "w:ins", "6", "修订五")  # 6
    p6 = document.add_paragraph()
    _revision(document, p6, "w:del", "7", "修订六", deleted=True)  # 7
    p7 = document.add_paragraph()
    _revision(document, p7, "w:ins", "8", "修订七")  # 8
    document.save(output)
    with zipfile.ZipFile(output) as archive:
        files = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    settings = files["word/settings.xml"]
    settings = re.sub(rb"(<w:settings[^>]*>)", rb"\1<w:trackChanges/>", settings, count=1)
    files["word/settings.xml"] = settings
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)


def _comments(output: Path) -> None:
    document = Document()
    for i, (target, text) in enumerate(
        [("关键一", "批注一内容"), ("关键二", "批注二内容"), ("关键三", "批注三内容")],
        start=1,
    ):
        paragraph = document.add_paragraph()
        run = paragraph.add_run(target)
        document.add_comment(run, text=text, author="审稿人")
    document.save(output)


def _norm(output: Path) -> None:
    document = Document()
    document.add_paragraph("水H₂O 温度25°C Ca²⁺ 与 H₂SO₄ 反应。")
    document.add_paragraph("平方公式 a² + b² = c²。")
    document.save(output)


def _large(output: Path) -> None:
    document = Document()
    for index in range(3000):
        if index == 1500:
            document.add_paragraph("大文档锚点词")
        else:
            document.add_paragraph(f"段落 {index} 内容。")
    table = document.add_table(rows=10, cols=10)
    for row in range(10):
        for col in range(10):
            table.cell(row, col).text = f"L{row}{col}"
    document.save(output)


def _review(output: Path) -> None:
    """Comments + pending revisions + trackChanges off: the ambiguous-signal
    document used by the comment-preservation acceptance case."""
    document = Document()
    for target, text in [("关键一", "批注一内容"), ("关键二", "批注二内容"), ("关键三", "批注三内容")]:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(target)
        document.add_comment(run, text=text, author="审稿人")
    _revision(document, document.paragraphs[0], "w:ins", "21", "修订甲")
    _revision(document, document.paragraphs[1], "w:del", "22", "修订乙", deleted=True)
    document.save(output)


BUILDERS = {
    "plain.docx": _plain,
    "styled.docx": _styled,
    "anchors.docx": _anchors,
    "table.docx": _table,
    "boxes.docx": _boxes,
    "parts.docx": _parts,
    "revisions.docx": _revisions,
    "comments.docx": _comments,
    "review.docx": _review,
    "norm.docx": _norm,
    "large.docx": _large,
}


_FIXED_TIME = (2026, 8, 8, 0, 0, 0)


def _canonicalize(path: Path) -> None:
    """Rewrite a generated docx with fixed zip timestamps and fixed core
    properties so regeneration is byte-stable across platforms and days."""
    import io

    with zipfile.ZipFile(path) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    core = entries.get("docProps/core.xml", b"")
    if core:
        core = re.sub(
            rb"<dcterms:created[^>]*>.*?</dcterms:created>",
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{_FIXED_TIME[0]:04d}-{_FIXED_TIME[1]:02d}-{_FIXED_TIME[2]:02d}T00:00:00Z</dcterms:created>'.encode(),
            core,
        )
        core = re.sub(
            rb"<dcterms:modified[^>]*>.*?</dcterms:modified>",
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{_FIXED_TIME[0]:04d}-{_FIXED_TIME[1]:02d}-{_FIXED_TIME[2]:02d}T00:00:00Z</dcterms:modified>'.encode(),
            core,
        )
        entries["docProps/core.xml"] = core
    for name in list(entries):
        if name.startswith("word/"):
            entries[name] = re.sub(
                rb'w:date="[^"]*"',
                f'w:date="{_FIXED_TIME[0]:04d}-{_FIXED_TIME[1]:02d}-{_FIXED_TIME[2]:02d}T00:00:00Z"'.encode(),
                entries[name],
            )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def generate(outdir: str | Path) -> Path:
    out = Path(outdir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    from scripts.create_complex_fixture import create_fixture

    for name, builder in BUILDERS.items():
        target = out / name
        builder(target)
        _canonicalize(target)
    complex_target = out / "complex.docx"
    create_fixture(complex_target)
    _canonicalize(complex_target)
    return out


MODEL_MANIFEST = "corpus/release/model-manifest.json"


def _model_hash(docx_path: Path, work_root: Path) -> str:
    """Hash of the extracted typed model (typed.md + paragraph records +
    styles) — rsid attributes and environment noise are normalized away."""
    import subprocess

    workdir = work_root / docx_path.stem
    subprocess.run(
        ["python", "-m", "scripts", "extract", str(docx_path), "-o", str(workdir)],
        cwd=REPO_ROOT, check=True, capture_output=True,
    )
    hasher = hashlib.sha256()
    for name in ("typed.md", "styles.json"):
        hasher.update((workdir / name).read_bytes())
    fmt = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
    records = json.dumps(fmt.get("paragraphs", []), ensure_ascii=False, sort_keys=True)
    hasher.update(records.encode("utf-8"))
    return hasher.hexdigest()[:16]


def check_models(release_dir: str | Path, work_root: str | Path, *, write: bool = False) -> int:
    """Compare the extracted typed models of the fixtures against the
    committed manifest (or write it with --write)."""
    release = Path(release_dir).resolve()
    work = Path(work_root).resolve()
    work.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(MODEL_MANIFEST).resolve()
    current = {
        path.name: _model_hash(path, work)
        for path in sorted(release.glob("*.docx"))
    }
    if write:
        manifest_path.write_text(
            json.dumps({"schema": "docx2typed-fixture-model-1", "fixtures": current}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"model manifest written: {manifest_path}")
        return 0
    committed = json.loads(manifest_path.read_text(encoding="utf-8"))["fixtures"]
    mismatches = {name: (committed.get(name), current[name]) for name in current if committed.get(name) != current[name]}
    if mismatches:
        for name, (expected, got) in mismatches.items():
            print(f"MISMATCH {name}: expected {expected} got {got}")
        return 1
    print(f"model manifest OK ({len(current)} fixtures)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate corpus/release fixtures")
    parser.add_argument("--outdir", default="corpus/release")
    parser.add_argument("--check-models", action="store_true", help="verify regenerated fixtures' typed models against the committed manifest")
    parser.add_argument("--write-models", action="store_true", help="write the typed-model manifest for the current fixtures")
    parser.add_argument("--work", default="/tmp/fixture-models")
    args = parser.parse_args()
    if args.check_models or args.write_models:
        return check_models(args.outdir, args.work, write=args.write_models)
    out = generate(args.outdir)
    print(f"generated {len(BUILDERS) + 1} fixtures in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
