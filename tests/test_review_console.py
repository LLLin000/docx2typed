from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scripts.review_console import render_document_fragment, render_html


COMMENTS_XML = """<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="0" w:author="reviewer" w:date="2026-01-01T00:00:00Z">
    <w:p><w:r><w:t>批注不应进入正文</w:t></w:r></w:p>
  </w:comment>
</w:comments>"""


def test_render_document_fragment_excludes_non_body_parts(tmp_path: Path):
    (tmp_path / "typed.md").write_text(
        "\n".join(
            [
                '<!--@typed schema="1" format="format.json" styles="styles.json" template="_template.docx" source="source.docx"-->',
                '<!--@p id="P0" base="s1"-->',
                '<docx-inline id="N0" kind="lastRenderedPageBreak"/>正文内容。',
                '<!--@part key="comments"-->',
                '<!--@p id="comments.P0" base="s1"-->',
                "批注不应进入正文",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "styles.json").write_text(
        json.dumps({"styles": {"s1": {"features": {}, "label": "normal"}}}),
        encoding="utf-8",
    )
    with zipfile.ZipFile(tmp_path / "_template.docx", "w") as archive:
        archive.writestr("word/comments.xml", COMMENTS_XML)

    fragment = render_document_fragment(tmp_path)

    assert "正文内容。" in fragment["html"]
    assert "批注不应进入正文" not in fragment["html"]
    assert "page-break" not in fragment["html"]
    assert fragment["comments"][0]["text"] == "批注不应进入正文"
    assert 'data-cstart="0"' in fragment["html"]
    assert 'data-cend="5"' in fragment["html"]
    assert 'data-editable="true"' in fragment["html"]

    page = render_html(tmp_path)
    assert 'id="workflow-strip"' in page
    assert 'data-flow-step="deliver"' in page
    assert "只有 build、verify 和 LibreOffice 检查通过才算交付" in page
