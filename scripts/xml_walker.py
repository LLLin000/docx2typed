"""Byte-level OOXML tag scanning: the single owner of the raw-XML token
discipline shared by every structural pass in the typed pipeline.

One module owns every hazard that has produced byte-corruption bugs:

- comment / CDATA / PI filtering (never treat ``<!-- … -->`` as a tag),
- self-closing detection (``<w:p/>``),
- namespace-prefix splitting (``w:p`` -> local name ``p``; matching is
  prefix-agnostic, so alternate prefixes like ``ns2:p`` behave identically),
- byte offsets (never str offsets — CJK text is multi-byte and would
  corrupt slicing),
- nesting-safe element range queries (``w:pPr`` inside
  ``w:pPrChange > w:pPr`` must return the outer element).

Consumers receive :class:`Tag` tokens with byte ranges and, where they
need the open-element stack, a :class:`TagCursor` that maintains it.  They
never re-derive the token discipline themselves.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

_TAG_RE = re.compile(rb"<!--.*?-->|<[^>]+>", re.DOTALL)
_START_TAG_RE = re.compile(rb"<\s*([A-Za-z_][A-Za-z0-9_.:-]*)(?:\s[^>]*?)?/?>")
_CLOSE_TAG_RE = re.compile(rb"</\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*>")


@dataclass(frozen=True)
class Tag:
    """One structural token in the raw bytes."""

    name: str  # local name, prefix stripped ("p" for "w:p")
    raw_name: str  # qname exactly as written ("w:p")
    closing: bool  # closing tag (</w:p>)
    self_closing: bool  # self-closing tag (<w:p/>)
    start: int  # byte offset of '<'
    end: int  # byte offset just past '>'

    def bytes_in(self, xml: bytes) -> bytes:
        """The token bytes (identical to the source slice)."""
        return xml[self.start:self.end]


def parse_tag(token: bytes, start: int, end: int) -> Tag | None:
    """Classify one raw token; None for comments, PIs, and CDATA."""
    if token.startswith(b"<!--") or token.startswith(b"<?") or token.startswith(b"<!["):
        return None
    closing = bool(re.match(rb"<\s*/", token))
    if closing:
        match = _CLOSE_TAG_RE.fullmatch(token)
        if not match:
            return None
        raw_name = match.group(1).decode("ascii")
        return Tag(raw_name.rsplit(":", 1)[-1], raw_name, True, False, start, end)
    match = _START_TAG_RE.fullmatch(token)
    if not match:
        return None
    raw_name = match.group(1).decode("ascii")
    return Tag(
        raw_name.rsplit(":", 1)[-1], raw_name, False, token.rstrip().endswith(b"/>"), start, end
    )


def iter_tags(xml: bytes, start: int = 0, end: int | None = None) -> Iterator[Tag]:
    """Yield every structural tag in ``xml[start:end]`` as a ``Tag``.

    The scan stops at the first tag starting at or past ``end``.  Byte
    offsets are always byte offsets; never convert them to str indices.
    """
    limit = len(xml) if end is None else end
    for match in _TAG_RE.finditer(xml):
        tag_start = match.start()
        if tag_start < start:
            continue
        if tag_start >= limit:
            return
        tag = parse_tag(match.group(0), tag_start, match.end())
        if tag is not None:
            yield tag


class TagCursor:
    """One walk over raw bytes with the open-element stack maintained.

    ``stack`` holds open elements as ``(name, start)`` pairs, outermost
    first.  For a closing tag the element being closed is still on the
    stack when the tag is yielded (``len(stack)`` is its depth); for an
    opening tag it is not yet pushed; self-closing tags never enter the
    stack.  After processing a closing tag the consumer calls :meth:`pop`,
    which returns the popped pair or None when the stack top does not
    match (the consumer owns the nesting-error policy).
    """

    def __init__(self, xml: bytes, start: int = 0, end: int | None = None) -> None:
        self._tags: Iterator[Tag] = iter_tags(xml, start, end)
        self.stack: list[tuple[str, int]] = []
        self.tag: Tag | None = None

    def __iter__(self) -> "TagCursor":
        return self

    def __next__(self) -> Tag:
        tag = next(self._tags)
        self.tag = tag
        return tag

    def pop(self) -> tuple[str, int] | None:
        """Pop the element being closed by the current closing tag.

        Returns ``(name, start)`` of the popped open element, or None when
        the stack top does not match the closing tag.
        """
        tag = self.tag
        if tag is None or not tag.closing:
            return None
        if not self.stack or self.stack[-1][0] != tag.name:
            return None
        return self.stack.pop()


def find_element_range(
    xml: bytes, name: str, start: int = 0, end: int | None = None,
) -> tuple[int, int] | None:
    """Byte range ``(open_start, close_end)`` of the first paired element
    with local name ``name``.

    Same-name nesting is honored: when ``w:pPr`` contains
    ``w:pPrChange > w:pPr``, the range returned is the OUTERMOST element.
    Self-closing elements are skipped (they have no close tag).
    """
    depth = 0
    open_start = -1
    for tag in iter_tags(xml, start, end):
        if tag.name != name or tag.self_closing:
            continue
        if tag.closing:
            depth -= 1
            if depth == 0:
                return (open_start, tag.end)
        else:
            if depth == 0:
                open_start = tag.start
            depth += 1
    return None


def find_open_tag_end(xml: bytes, name: str, start: int = 0, end: int | None = None) -> int:
    """Byte offset just past the first non-self-closing open tag ``name``,
    or -1 when absent."""
    for tag in iter_tags(xml, start, end):
        if tag.name == name and not tag.closing and not tag.self_closing:
            return tag.end
    return -1
