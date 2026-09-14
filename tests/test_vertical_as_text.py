"""docx2typed: vertical alignment as text (schema 2).

The contract (docs/prd/vertical-alignment-as-text.md): superscript/subscript is
a textual annotation of editable content **iff** the engine can factor
`w:vertAlign` out of the run formatting reversibly and losslessly. Eligibility
is canonical run-property identity — remove exactly one `w:vertAlign` and the
remainder must canonicalise equal to the paragraph's base style — never a
feature-dict diff, because `rpr_features` enumerates only part of Word's `rPr`
vocabulary.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from docx import Document

from scripts import main
from scripts.mcp_server import build_docx, commit_sync, format_span, session, workdir_open
from scripts.typed_core import (
    NS_W,
    Paragraph,
    StyleRegistry,
    TextNode,
    TypedError,
    content_signature,
    factor_vertical_style,
    merge_adjacent_text,
    parse_typed,
    promote_vertical_alignment,
    serialize_typed,
    vertical_style_variant,
)
from scripts.typed_docx import validate_workdir

HEAD = (
    '<!--@typed schema="{schema}" format="format.json" styles="styles.json" '
    'template="_template.docx" source="x.docx"-->\n'
)



def _code(result) -> str:
    envelope = _envelope(result)
    return (envelope.get("diagnostics") or [{}])[0].get("code") or "OK"


def _envelope(result):
    if isinstance(result, str):
        return json.loads(result)
    if isinstance(result, dict):
        return result
    return result.structuredContent


def _registry(*rprs: str) -> StyleRegistry:
    registry = StyleRegistry()
    for rpr in rprs:
        registry.ensure(rpr)
    return registry


def _rpr(*children: str) -> str:
    return f'<w:rPr xmlns:w="{NS_W}">' + "".join(children) + "</w:rPr>"


BODY_FONT = '<w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体"/>'
SUPER = '<w:vertAlign w:val="superscript"/>'
SUB = '<w:vertAlign w:val="subscript"/>'


# ---------------------------------------------------------------------------
# The predicate: canonical identity, one element removed
# ---------------------------------------------------------------------------

def test_factors_a_pure_vertical_variant():
    registry = _registry(_rpr(BODY_FONT), _rpr(BODY_FONT, SUPER))
    base = registry.ensure(_rpr(BODY_FONT))
    variant = registry.ensure(_rpr(BODY_FONT, SUPER))
    assert factor_vertical_style(registry, variant, base) == ("superscript", base)


def test_refuses_when_something_else_differs_too():
    registry = _registry(_rpr(BODY_FONT), _rpr(BODY_FONT, SUPER), _rpr('<w:rFonts w:ascii="宋体"/>', SUPER))
    base = registry.ensure(_rpr(BODY_FONT))
    hinted = registry.ensure(_rpr('<w:rFonts w:ascii="宋体"/>', SUPER))
    # the font differs as well: textifying would silently drop it
    assert factor_vertical_style(registry, hinted, base) is None


def test_refuses_position_and_non_alignment_values():
    registry = _registry(
        _rpr(BODY_FONT),
        _rpr(BODY_FONT, '<w:position w:val="6"/>'),
        _rpr(BODY_FONT + '<w:vertAlign w:val="baseline"/>'),
    )
    base = registry.ensure(_rpr(BODY_FONT))
    positioned = registry.ensure(_rpr(BODY_FONT, '<w:position w:val="6"/>'))
    baseline = registry.ensure(_rpr(BODY_FONT, '<w:vertAlign w:val="baseline"/>'))
    assert factor_vertical_style(registry, positioned, base) is None
    assert factor_vertical_style(registry, baseline, base) is None


def test_refuses_two_vertalign_elements_and_the_base_itself():
    registry = _registry(_rpr(BODY_FONT), _rpr(BODY_FONT, SUPER, SUPER))
    base = registry.ensure(_rpr(BODY_FONT))
    doubled = registry.ensure(_rpr(BODY_FONT, SUPER, SUPER))
    assert factor_vertical_style(registry, doubled, base) is None
    assert factor_vertical_style(registry, base, base) is None


def test_variant_writer_and_predicate_are_inverses():
    """The writer creates the variant the predicate factors back — that is what
    keeps a promoted document byte-identical."""
    registry = _registry(_rpr(BODY_FONT))
    base = registry.ensure(_rpr(BODY_FONT))
    variant = vertical_style_variant(registry, base, "superscript")
    assert (variant, factor_vertical_style(registry, variant, base)) == (
        variant,
        ("superscript", base),
    )


# ---------------------------------------------------------------------------
# Schema 2: the text carries the alignment
# ---------------------------------------------------------------------------

def test_schema2_reads_tags_into_the_vertical_dimension():
    document = parse_typed(
        HEAD.format(schema="2")
        + '\n<!--@p id="P0" base="s_b"-->\n'
        + "Ca^{2+} 与 Ca_{3} 以及字面量 \\^{x}\n"
    )
    nodes = document.paragraphs[0].nodes
    assert [(node.text, node.vertical) for node in nodes] == [
        ("Ca", None),
        ("2+", "superscript"),
        (" 与 Ca", None),
        ("3", "subscript"),
        (" 以及字面量 ^{x}", None),
    ]


def test_schema2_serializes_the_dimension_back_to_tags():
    source = (
        HEAD.format(schema="2")
        + '\n<!--@p id="P0" base="s_b"-->\n'
        + "Ca^{2+} 与 Ca_{3}\n"
    )
    assert serialize_typed(parse_typed(source)) == source


def test_schema1_keeps_the_markers_literal():
    source = (
        HEAD.format(schema="1")
        + '\n<!--@p id="P0" base="s_b"-->\n'
        + "Ca^{2+}\n"
    )
    document = parse_typed(source)
    assert [(node.text, node.vertical) for node in document.paragraphs[0].nodes] == [
        ("Ca^{2+}", None)
    ]
    # a schema-1 source keeps its bytes: no escaping, no tags
    assert serialize_typed(document) == source


def test_unknown_schema_is_refused():
    with pytest.raises(TypedError, match="incompatible typed source schema"):
        parse_typed(HEAD.format(schema="3") + '\n<!--@p id="P0" base="s_b"-->\nx\n')


def test_nested_or_unclosed_tags_are_refused():
    for body in ("Ca^{2+", "Ca^{2^{3}}", "Ca^{}"):
        with pytest.raises(TypedError):
            parse_typed(HEAD.format(schema="2") + '\n<!--@p id="P0" base="s_b"-->\n' + body + '\n')


def test_merging_never_crosses_the_vertical_dimension():
    merged = merge_adjacent_text(
        [TextNode("s_b", "Ca"), TextNode("s_b", "2+", "superscript"), TextNode("s_b", " 离子")]
    )
    assert [(node.text, node.vertical) for node in merged] == [
        ("Ca", None),
        ("2+", "superscript"),
        (" 离子", None),
    ]


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

def test_promotion_moves_eligible_regions_only():
    registry = _registry(
        _rpr(BODY_FONT),
        _rpr(BODY_FONT, SUPER),
        _rpr('<w:rFonts w:ascii="宋体"/>', SUPER),
    )
    base = registry.ensure(_rpr(BODY_FONT))
    pure = registry.ensure(_rpr(BODY_FONT, SUPER))
    hinted = registry.ensure(_rpr('<w:rFonts w:ascii="宋体"/>', SUPER))
    document = parse_typed(
        HEAD.format(schema="1")
        + f'\n<!--@p id="P0" base="{base}"-->\n'
        + f'Ca<span data-s="{pure}">2+</span><span data-s="{hinted}">3+</span>\n'
    )
    promoted = promote_vertical_alignment(document, registry)
    assert promoted == 1
    kinds = [
        (node.text, node.style_id, node.vertical)
        for node in document.paragraphs[0].nodes
        if isinstance(node, TextNode)
    ]
    assert kinds == [("Ca", base, None), ("2+", base, "superscript"), ("3+", hinted, None)]


# ---------------------------------------------------------------------------
# End to end: extract promotes, the package keeps the property, byte-identical
# ---------------------------------------------------------------------------

def _superscript_document(path: Path) -> Path:
    document = Document()
    document.add_paragraph("前言。")
    paragraph = document.add_paragraph()
    paragraph.add_run("螯合的Ca")
    superscript = paragraph.add_run("2+")
    superscript.font.superscript = True
    paragraph.add_run("与Cu")
    subscript = paragraph.add_run("2+")
    subscript.font.subscript = True
    document.save(str(path))
    return path


def test_extract_promotes_and_the_built_package_still_carries_the_alignment(tmp_path):
    source = _superscript_document(tmp_path / "s.docx")
    workdir = tmp_path / "wd"
    assert main(["--json", "extract", str(source), "-o", str(workdir), "--operation-id", "e1"]) == 0

    typed = (workdir / "typed.md").read_text(encoding="utf-8")
    promotion = json.loads((workdir / "format.json").read_text(encoding="utf-8"))["vertical_promotion"]
    assert typed.splitlines()[0].startswith('<!--@typed schema="2"')
    assert "Ca^{2+}" in typed and "Cu_{2+}" in typed
    assert promotion["promoted"] == 2
    validate_workdir(workdir)

    session.workdir = None
    workdir_open(str(workdir), track=False)
    assert commit_sync(label="提升") is not None
    output = tmp_path / "built.docx"
    build_docx(output=str(output))

    import zipfile

    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert 'w:val="superscript"' in xml and 'w:val="subscript"' in xml
    # the markers themselves must never reach the package
    assert "^{" not in xml and "_{" not in xml


# ---------------------------------------------------------------------------
# The signature must see the dimension (a vertical-only change is a change)
# ---------------------------------------------------------------------------

def test_content_signature_sees_a_vertical_only_change():
    plain = Paragraph("P0", "s_b", [TextNode("s_b", "Ca2+")])
    superscript = Paragraph(
        "P0", "s_b", [TextNode("s_b", "Ca2+", "superscript")]
    )
    assert content_signature(plain) != content_signature(superscript)


def test_vertical_only_format_is_built_not_replayed(tmp_path):
    """A change that only adds the alignment, leaving every character alone, must
    reach the package: if the content signature ignored the dimension the build
    would treat the paragraph as untouched and replay the baseline bytes."""
    import zipfile

    source = tmp_path / "s.docx"
    document = Document()
    document.add_paragraph("第一段正文。")
    variant = document.add_paragraph()
    variant.add_run("2+").font.superscript = True  # gives the doc a superscript style
    document.save(str(source))
    workdir = tmp_path / "wd"
    assert main(["--json", "extract", str(source), "-o", str(workdir), "--operation-id", "e1"]) == 0

    session.workdir = None
    workdir_open(str(workdir), track=False)
    assert _code(format_span(
        paragraph_id="P0", old="第一段正文。", attributes={"vertAlign": "superscript"}
    )) == "OK"
    assert _code(commit_sync(label="仅上标")) == "OK"
    output = tmp_path / "built.docx"
    assert _code(build_docx(output=str(output))) == "OK"

    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    i = xml.find("第一段正文。")
    start = max(xml.rfind("<w:p>", 0, i), xml.rfind("<w:p ", 0, i))
    paragraph = xml[start : xml.find("</w:p>", i)]
    assert 'w:val="superscript"' in paragraph, paragraph[-260:]
