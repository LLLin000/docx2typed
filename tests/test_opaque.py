from docx import Document
from docx.oxml import OxmlElement

from build import build
from extract import extract


def test_touched_opaque_paragraph_fails_before_output(tmp_path):
    source = tmp_path / "source.docx"
    workdir = tmp_path / "workdir"
    output = tmp_path / "output.docx"
    document = Document()
    paragraph = document.add_paragraph("前缀")
    field = OxmlElement("w:fldSimple")
    field.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}instr", " PAGE ")
    field_run = OxmlElement("w:r")
    field_text = OxmlElement("w:t")
    field_text.text = "1"
    field_run.append(field_text)
    field.append(field_run)
    paragraph._p.append(field)
    document.save(source)

    assert extract([str(source), "-o", str(workdir)]) == 0
    typed_path = workdir / "typed.md"
    typed = typed_path.read_text(encoding="utf-8")
    assert "docx-opaque" in typed
    typed_path.write_text(typed.replace("前缀", "修改"), encoding="utf-8")
    assert build([str(workdir), "-o", str(output)]) == 1
    assert not output.exists()
