from docx import Document

from scripts.build import build
from scripts.extract import extract
from scripts.typed_core import TypedError
from scripts.typed_docx import validate_workdir


def test_build_rejects_missing_tombstone_and_unknown_style(tmp_path):
    source = tmp_path / "source.docx"
    workdir = tmp_path / "workdir"
    output = tmp_path / "output.docx"
    document = Document()
    document.add_paragraph("第一段")
    document.add_paragraph("第二段")
    document.save(source)
    assert extract([str(source), "-o", str(workdir)]) == 0

    blocks = (workdir / "typed.md").read_text(encoding="utf-8").split("\n\n")
    header, _, p1 = blocks[:3]
    (workdir / "typed.md").write_text("\n\n".join([header, p1]) + "\n", encoding="utf-8")
    assert build([str(workdir), "-o", str(output)]) == 1
    assert not output.exists()

    extract([str(source), "-o", str(workdir)])
    typed_path = workdir / "typed.md"
    typed = typed_path.read_text(encoding="utf-8")
    typed_path.write_text(typed.replace("第一段", '<span data-s="missing-style">第一段</span>'), encoding="utf-8")
    assert build([str(workdir), "-o", str(output)]) == 1
    assert not output.exists()


def test_validate_rejects_malformed_typed_markup(tmp_path):
    source = tmp_path / "source.docx"
    workdir = tmp_path / "workdir"
    document = Document()
    document.add_paragraph("正文")
    document.save(source)
    assert extract([str(source), "-o", str(workdir)]) == 0
    typed_path = workdir / "typed.md"
    typed_path.write_text(typed_path.read_text(encoding="utf-8").replace("正文", "正文<span>未闭合"), encoding="utf-8")

    try:
        validate_workdir(workdir)
    except TypedError as exc:
        assert "unknown" in str(exc) or "span" in str(exc)
    else:
        raise AssertionError("malformed typed markup was accepted")
