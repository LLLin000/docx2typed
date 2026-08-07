"""XmlWalker: the shared byte-level token discipline (PRD byte-surgery-layer).

The walker owns every byte-level hazard that has produced corruption bugs:
CJK byte offsets, self-closing tags, namespace prefixes, comments/PI/CDATA
filtering, and nesting-safe element ranges.  These tests pin that
discipline so per-operation tests stop re-testing the accidents.
"""
from __future__ import annotations

from scripts.xml_walker import (
    Tag,
    TagCursor,
    find_element_range,
    find_open_tag_end,
    iter_tags,
    parse_tag,
)


def _names(xml: bytes, **kwargs) -> list[str]:
    return [tag.name for tag in iter_tags(xml, **kwargs)]


def _tags(xml: bytes) -> list[Tag]:
    return list(iter_tags(xml))


def test_parse_tag_classifies_open_close_self_closing():
    assert parse_tag(b"<w:p>", 0, 5) == Tag("p", "w:p", False, False, 0, 5)
    assert parse_tag(b"</w:p>", 0, 6) == Tag("p", "w:p", True, False, 0, 6)
    assert parse_tag(b"<w:p/>", 0, 6) == Tag("p", "w:p", False, True, 0, 6)
    assert parse_tag(b"<w:p />", 0, 7) == Tag("p", "w:p", False, True, 0, 7)


def test_parse_tag_skips_comments_pi_cdata():
    assert parse_tag(b"<!-- note -->", 0, 13) is None
    assert parse_tag(b"<?xml version='1.0'?>", 0, 21) is None
    assert parse_tag(b"<![CDATA[x]]>", 0, 14) is None


def test_iter_tags_prefix_agnostic_local_names():
    xml = b"<w:body><ns2:p><w:r><ns3:t>text</ns3:t></w:r></ns2:p></w:body>"
    assert _names(xml) == ["body", "p", "r", "t", "t", "r", "p", "body"]


def test_iter_tags_byte_offsets_with_cjk_text():
    """CJK code points are multi-byte; offsets must stay byte offsets."""
    xml = "前<w:p>中文段落内容</w:p>后".encode("utf-8")
    tags = _tags(xml)
    p_open = tags[0]
    p_close = tags[1]
    assert p_open.name == "p" and not p_open.closing
    assert p_close.name == "p" and p_close.closing
    # the opening tag's end lands before the CJK text bytes
    assert xml[p_open.start:p_open.end] == b"<w:p>"
    assert xml[p_close.start:p_close.end] == b"</w:p>"
    # closing start must be the byte offset of '<' — slicing the middle
    # must yield exactly the CJK text, not shifted bytes
    assert xml[p_open.end:p_close.start].decode("utf-8") == "中文段落内容"


def test_iter_tags_range_window():
    xml = b"<w:body><w:p>A</w:p><w:p>B</w:p><w:p>C</w:p></w:body>"
    mid = xml.index(b"<w:p>B</w:p>")
    names = _names(xml, start=mid)
    assert names == ["p", "p", "p", "p", "body"]
    names_limited = _names(xml, start=mid, end=xml.index(b"<w:p>C</w:p>"))
    assert names_limited == ["p", "p"]


def test_iter_tags_self_closing_never_enters_stack():
    xml = b"<w:body><w:p><w:r/><w:t>x</w:t></w:p></w:body>"
    cursor = TagCursor(xml)
    depths: list[tuple[str, int]] = []
    for tag in cursor:
        if tag.closing:
            depths.append((tag.name, len(cursor.stack)))
            cursor.pop()
        elif not tag.self_closing:
            depths.append((tag.name, len(cursor.stack)))
            cursor.stack.append((tag.name, tag.start))
    assert depths == [
        ("body", 0), ("p", 1), ("t", 2), ("t", 3), ("p", 2), ("body", 1),
    ]


def test_cursor_pop_mismatch_returns_none():
    cursor = TagCursor(b"<w:body><w:p></w:r></w:body>")
    tags: list[Tag] = []
    for tag in cursor:
        tags.append(tag)
        if tag.closing:
            cursor.pop()
    assert tags[1].name == "p"
    assert tags[2].name == "r" and tags[2].closing
    assert cursor.pop() is None  # </w:r> cannot close <w:p>


def test_find_element_range_outermost_with_nesting():
    """w:pPr inside w:pPrChange > w:pPr must resolve to the outer element."""
    xml = (
        b"<w:p><w:pPr><w:pStyle w:val=\"a\"/><w:pPrChange>"
        b"<w:pPr><w:pStyle w:val=\"b\"/></w:pPr>"
        b"</w:pPrChange></w:pPr><w:r><w:t>x</w:t></w:r></w:p>"
    )
    rng = find_element_range(xml, "pPr")
    assert rng is not None
    start, end = rng
    assert xml[start:end] == (
        b"<w:pPr><w:pStyle w:val=\"a\"/><w:pPrChange>"
        b"<w:pPr><w:pStyle w:val=\"b\"/></w:pPr>"
        b"</w:pPrChange></w:pPr>"
    )


def test_find_element_range_skips_self_closing():
    xml = b"<w:tc><w:tcPr/><w:p>x</w:p></w:tc>"
    rng = find_element_range(xml, "tcPr")
    assert rng is None
    rng_p = find_element_range(xml, "p")
    assert rng_p is not None and xml[rng_p[0]:rng_p[1]] == b"<w:p>x</w:p>"


def test_find_element_range_absent():
    assert find_element_range(b"<w:p>x</w:p>", "tcPr") is None
    assert find_element_range(b"", "p") is None


def test_find_open_tag_end():
    xml = b"<w:tc><w:tcPr><w:tcW w:w=\"800\"/></w:tcPr><w:p/></w:tc>"
    assert find_open_tag_end(xml, "tc") == xml.index(b">") + 1
    assert find_open_tag_end(xml, "p") == -1  # only self-closing present
    assert find_open_tag_end(xml, "tcPr") == xml.index(b"<w:tcW")


def test_bytes_in_roundtrip():
    xml = b"<w:p><w:r><w:t>hi</w:t></w:r></w:p>"
    tag = _tags(xml)[0]
    assert tag.bytes_in(xml) == b"<w:p>"


def test_iter_tags_comment_inside_prose_not_mistaken_for_tag():
    xml = b"<w:p>a<!-- x -->b<w:t>c</w:t></w:p>"
    assert _names(xml) == ["p", "t", "t", "p"]
