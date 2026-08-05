from scripts.typed_core import parse_typed, project_clean, serialize_typed


HEADER = '<!--@typed schema="1" format="format.json" styles="styles.json" template="_template.docx" source="source.docx"-->'


def test_typed_text_round_trip_preserves_literal_markup_and_style_span():
    source = (
        HEADER
        + "\n\n"
        + '<!--@p id="P0" base="s_body"-->\nA &amp; &lt; B'
        + '<span data-s="s_bold">加粗</span>'
        + "\n\n"
        + '<!--@p id="P1" base="s_body"-->\n第二段'
        + "\n"
    )

    document = parse_typed(source)

    assert project_clean(document, markers=False) == "A & < B加粗\n第二段"
    assert serialize_typed(document).startswith(HEADER + "\n\n")
    assert '<span data-s="s_bold">加粗</span>' in serialize_typed(document)
