from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document

from build import build
from extract import extract
from verify import verify


def test_verify_rejects_semantically_equal_untouched_xml_rewrite(tmp_path):
    source = tmp_path / "source.docx"
    workdir = tmp_path / "workdir"
    output = tmp_path / "output.docx"
    tampered = tmp_path / "tampered.docx"
    document = Document()
    document.add_paragraph("不变")
    document.save(source)

    assert extract([str(source), "-o", str(workdir)]) == 0
    assert build([str(workdir), "-o", str(output)]) == 0
    with ZipFile(output) as archive:
        document_xml = archive.read("word/document.xml").replace(b"<w:t>", b'<w:t xml:space="preserve">', 1)
        with ZipFile(tampered, "w", ZIP_DEFLATED) as rebuilt:
            for info in archive.infolist():
                rebuilt.writestr(info, document_xml if info.filename == "word/document.xml" else archive.read(info.filename))

    assert verify([str(workdir), str(tampered)]) == 1
