from zipfile import ZipFile

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from scripts.build import build
from scripts.extract import extract
from scripts.verify import verify


def test_deleting_earlier_paragraph_keeps_later_structural_tokens(tmp_path):
    source = tmp_path / "source.docx"
    workdir = tmp_path / "workdir"
    output = tmp_path / "output.docx"
    document = Document()
    document.add_paragraph("删除")
    paragraph = document.add_paragraph("前")
    relationship_id = document.part.relate_to("https://example.com", RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "链接"
    run.append(text)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)
    commented_run = paragraph.add_run("批注")
    document.add_comment(commented_run, text="保留", author="tester")
    document.save(source)

    assert extract([str(source), "-o", str(workdir)]) == 0
    blocks = (workdir / "typed.md").read_text(encoding="utf-8").split("\n\n")
    header, _, p1 = blocks[:3]
    (workdir / "typed.md").write_text("\n\n".join([header, p1, '<!--@delete id="P0"-->']) + "\n", encoding="utf-8")

    assert build([str(workdir), "-o", str(output)]) == 0
    assert verify([str(workdir), str(output)]) == 0
    with ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
        assert "w:hyperlink" in xml and "w:commentRangeStart" in xml
