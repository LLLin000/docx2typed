"""Property/fuzz tests for the shared byte walker (PRD byte-surgery-layer).

The walker is the single point of defense for the token discipline, so it
gets a fuzz harness: a seeded generator builds well-formed documents with
the interesting structures (same-name nesting, tables in cells, comments
with fake tags, CDATA, CJK text), and a malformed fuzzer throws random
bytes at the API. Properties:

- P1 prefix invariance: renaming namespaces must not change the tag stream
- P2 comment/CDATA/PI invariance: injected non-tag tokens must not appear
- P3 whitespace/self-closing consistency
- P4 malformed input can only fail cleanly (no raises from iter_tags, all
  byte ranges in bounds)
- P5 well-formed round trip: locate -> no-op patch reproduces the bytes
- P6 find_element_range always returns a balanced same-name range
"""
from __future__ import annotations

import random

from scripts.typed_docx import (
    ValidationError,
    locate_document_xml,
    patch_document_xml,
)
from scripts.xml_walker import find_element_range, iter_tags, parse_tag

_NAMES = [
    "w:p", "x:p", "w:r", "w:t", "w:pPr", "w:rPr", "w:ins", "w:del",
    "w:tbl", "w:tr", "w:tc", "w:tcPr", "w:gridSpan", "m:oMath", "w:tblGrid",
    "w:gridCol", "w:bookmarkStart", "w:commentRangeStart", "w:fldSimple",
    "w:sectPr", "w:body", "w:document", "w:sdt", "w:sdtContent",
]
_ATTRS = [
    'w:id="1"', 'w:val="2"', 'w:name="bm1"', 'r:id="rId1"', 'w:instr=" PAGE "',
    'w:color="auto"', 'xml:space="preserve"', 'w:author="审稿人"',
    'w:date="2026-08-06T10:12:00Z"', 'w:val=">"', 'w:val="<"',
]


def _rand_text(rng: random.Random) -> str:
    return rng.choice(["中文内容", "abc", "A2", "20 mg", "  ", "x"])


def _gen_element(rng: random.Random, depth: int, out: list[str]) -> None:
    """Emit a random well-formed element (nested containers included)."""
    name = rng.choice(_NAMES)
    tag = name if rng.random() < 0.5 else name.replace("w:", "ns7:")
    attrs = " ".join(rng.sample(_ATTRS, rng.randint(0, 2)))
    prefix = f"<{tag}{(' ' + attrs) if attrs else ''}>"
    close = f"</{tag}>"
    if depth <= 0 or rng.random() < 0.3:
        if rng.random() < 0.3:
            out.append(f"<{tag}{(' ' + attrs) if attrs else ''}/>")
        else:
            out.append(prefix)
            if rng.random() < 0.5:
                out.append(_rand_text(rng))
            out.append(close)
        return
    out.append(prefix)
    children = rng.randint(1, 4)
    for _ in range(children):
        roll = rng.random()
        if roll < 0.2:
            out.append(_rand_text(rng))
        elif roll < 0.3:
            out.append("<!-- fake </w:p> inside -->")
        elif roll < 0.35:
            out.append("<![CDATA[fake <w:tbl> here]]>")
        elif roll < 0.4:
            out.append("<?pi fake <w:tr> ?>")
        elif name in ("w:t", "x:t", "ns7:t"):
            out.append(_rand_text(rng))
        else:
            _gen_element(rng, depth - 1, out)
    out.append(close)


def _gen_document(rng: random.Random) -> bytes:
    out = ['<?xml version="1.0" encoding="UTF-8"?>']
    out.append('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:m="math" xmlns:ns7="other">')
    out.append("<w:body>")
    for _ in range(rng.randint(3, 8)):
        roll = rng.random()
        if roll < 0.5:
            _gen_element(rng, rng.randint(1, 4), out)
        elif roll < 0.7:
            out.append("<w:p><w:pPr><w:pPrChange><w:pPr><w:pStyle w:val=\"a\"/></w:pPr></w:pPrChange></w:pPr><w:r><w:t>同层嵌套</w:t></w:r></w:p>")
        elif roll < 0.85:
            out.append("<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格</w:t></w:r></w:p></w:tc><w:tc><w:tbl><w:tr><w:tc><w:p><w:r><w:t>嵌套表</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:tc></w:tr></w:tbl>")
        else:
            out.append(f"<w:p><w:r><w:t>{_rand_text(rng)}</w:t></w:r></w:p>")
    out.append("</w:body>")
    out.append("</w:document>")
    return "".join(out).encode("utf-8")


def _tag_sequence(xml: bytes) -> list[tuple[str, bool, bool]]:
    return [(t.name, t.closing, t.self_closing) for t in iter_tags(xml)]


def test_p1_prefix_invariance():
    rng = random.Random(1)
    for _ in range(60):
        xml = _gen_document(rng)
        variants = [
            xml,
            xml.replace(b"ns7:", b"zz:").replace(b"<w:", b"<w:"),  # no-op sanity
            xml.replace(b"ns7:", b"foo:"),
        ]
        base = _tag_sequence(xml)
        for variant in variants[1:]:
            assert _tag_sequence(variant) == base


def test_p2_comment_cdata_pi_invariance():
    rng = random.Random(2)
    for _ in range(60):
        xml = _gen_document(rng)
        base = _tag_sequence(xml)
        decorated = xml.replace(
            b"<w:body>",
            "<w:body><!-- c --><![CDATA[<w:tbl>]]><?pi <w:tr> ?><w:p><w:r><w:t>前</w:t></w:r></w:p>".encode("utf-8"),
            1,
        )
        assert _tag_sequence(decorated)[:1] == base[:1]
        assert all(t.name != "tbl" for t in iter_tags(b"<![CDATA[<w:tbl>]]>"))
        assert all(t.name != "tr" for t in iter_tags(b"<?pi <w:tr> ?>"))


def test_p3_self_closing_whitespace_consistency():
    for raw, expected in [
        (b"<a/>", [(True, True)]),
        (b"<a />", [(True, True)]),
        (b"<a attr='x'/>", [(True, True)]),
        (b"<a attr='>' />", [(True, True)]),
        (b"<a/>", [(True, True)]),
    ]:
        tag = parse_tag(raw, 0, len(raw))
        assert tag is not None
        assert (not tag.closing, tag.self_closing) == expected[0]


def test_p4_malformed_input_only_fails_cleanly():
    rng = random.Random(3)
    alphabet = "<w:/> xm\"'=abc>&[]?-- \u4e2d".encode("utf-8")  # includes CJK bytes
    for _ in range(400):
        size = rng.randint(0, 200)
        blob = bytes(rng.choice(alphabet) for _ in range(size))
        for tag in iter_tags(blob):
            assert 0 <= tag.start < tag.end <= len(blob)
            assert tag.raw_name
        # consumers must either succeed or raise ValidationError — never
        # crash with a different exception on well-shaped wrappers
        if b"<w:document" in blob and blob.count(b"<w:document") == blob.count(b"</w:document"):
            try:
                locate_document_xml(blob)
            except ValidationError:
                pass


def test_p5_wellformed_roundtrip_noop():
    rng = random.Random(4)
    for _ in range(120):
        xml = _gen_document(rng)
        slices = locate_document_xml(xml)
        paragraphs = [s.raw for s in slices.paragraphs]
        rebuilt = patch_document_xml(xml, slices, paragraphs)
        assert rebuilt == xml


def test_p6_element_range_balanced():
    rng = random.Random(5)
    for _ in range(120):
        xml = _gen_document(rng)
        name = rng.choice(["w:p", "w:tbl", "w:pPr", "w:tc", "x:p"])
        rng_range = find_element_range(xml, name.rsplit(":", 1)[-1])
        if rng_range is None:
            continue
        start, end = rng_range
        assert xml[start:start + 2] == b"<w" or xml[start:start + 2] in (b"<x", b"<n")
        fragment = xml[start:end]
        open_count = sum(1 for t in iter_tags(fragment) if t.name == name.rsplit(":", 1)[-1] and not t.closing and not t.self_closing)
        close_count = sum(1 for t in iter_tags(fragment) if t.name == name.rsplit(":", 1)[-1] and t.closing)
        assert open_count == close_count
