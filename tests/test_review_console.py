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

    assert 'class="comment-detail-body"' in page
    assert "批注内容" in page
    assert "仅发送处理指令；原始批注保持不变。" in page
    assert "--font-ui:" in page
    assert "[hidden]" in page


def test_render_document_fragment_groups_table_cells(tmp_path: Path):
    (tmp_path / "typed.md").write_text(
        "\n".join(
            [
                '<!--@typed schema="1" format="format.json" styles="styles.json" template="_template.docx" source="source.docx"-->',
                '<!--@p id="P0" base="s1"-->',
                "前文",
                '<!--@p id="T0.R0.C0.P0" base="s1"-->',
                "阶段",
                '<!--@p id="T0.R0.C1.P0" base="s1"-->',
                "输入",
                '<!--@p id="T0.R1.C0.P0" base="s1"-->',
                "提取",
                '<!--@p id="T0.R1.C1.P0" base="s1"-->',
                "source.docx",
                '<!--@p id="P1" base="s1"-->',
                "后文",
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
        archive.writestr("word/document.xml", "<w:document/>")

    fragment = render_document_fragment(tmp_path)
    html = str(fragment["html"])

    assert html.count('<table class="document-table"') == 1
    assert html.count('<tr class="document-table-row') == 2
    assert 'data-pid="T0.R0.C0.P0"' in html
    assert 'data-pid="T0.R1.C1.P0"' in html
    assert html.index("前文") < html.index('<table class="document-table"') < html.index("后文")
    assert 'data-table-id="T0"' in html
    assert "1 张表" in render_html(tmp_path)