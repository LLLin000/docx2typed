"""rust_tracer59_oracle.py - issue #59 differential oracle.

Computes the semantic signatures that MUST match between the Rust tracer
chain and the Python Reference, using the Python Reference model itself:

  revision-inventory  - per-revision (part, kind, w_id, author, date, text,
                        fingerprint, revision_key) via
                        scan_package_revisions + the revision_key rule
                        (sha256(text)[:12]).
  view-text           - per-paragraph visible text after
                        settle_xml_revisions(xml, action) (the same byte
                        settlement both chains must reproduce).
  comment-inventory   - (id, author, date, text) from word/comments.xml.
  table-cells         - per body-level table, the row/col visible-text
                        matrix (structure + text signature after a table op).
  unicode-candidates  - count of vertical-catalog candidates in document.xml
                        w:t leaves (find_candidates semantics).

Usage:
  python rust_tracer59_oracle.py <kind> <docx> [action] [table-index]
"""
import hashlib
import json
import re
import sys
import zipfile

sys.path.insert(0, __file__.rsplit("\\", 1)[0] + "/..")

from scripts.typed_docx import (  # noqa: E402
    PART_KEYS_PATTERN,
    locate_document_xml,
    scan_package_revisions,
    settle_xml_revisions,
)

NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _revision_key(part, kind, w_id, text):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{part}|{kind}|{w_id}|{digest}"


def revision_inventory(path):
    entries = scan_package_revisions(path)
    out = []
    for entry in entries:
        out.append({
            "part": entry["part"],
            "kind": entry["kind"],
            "w_id": entry["w_id"],
            "author": entry["author"],
            "date": entry["date"],
            "text": entry["text"],
            "fingerprint": hashlib.sha256(entry["text"].encode("utf-8")).hexdigest()[:12],
            "revision_key": _revision_key(
                entry["part"], entry["kind"], entry["w_id"], entry["text"]
            ),
        })
    return out


def _paragraph_texts(xml):
    """Visible text per w:p in a settled document."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)
    out = []
    for paragraph in root.iter(NS_W + "p"):
        text = "".join(
            child.text or ""
            for child in paragraph.iter()
            if child.tag in (NS_W + "t", NS_W + "delText")
        )
        out.append(text)
    return out


def view_text(path, action):
    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml")
    settled = settle_xml_revisions(document, action)
    return _paragraph_texts(settled)


def comment_inventory(path):
    with zipfile.ZipFile(path) as archive:
        try:
            raw = archive.read("word/comments.xml")
        except KeyError:
            return []
    import xml.etree.ElementTree as ET

    root = ET.fromstring(raw)
    out = []
    for comment in root.findall(NS_W + "comment"):
        out.append({
            "id": comment.get(NS_W + "id"),
            "author": comment.get(NS_W + "author", ""),
            "date": comment.get(NS_W + "date", ""),
            "text": "".join(comment.itertext()).strip(),
        })
    return out


def table_cells(path, table_index):
    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml")
    slices = locate_document_xml(document)
    body_tables = [t for t in slices.tables if t.body_level]
    if table_index >= len(body_tables):
        return {"error": f"table {table_index} not found"}
    table = body_tables[table_index]
    raw = document[table.start:table.end]
    text = raw.decode("utf-8", errors="replace")
    # row text signature: strip tags and whitespace per <w:tr>...</w:tr>.
    rows = []
    for row in re.findall(r"<w:tr>.*?</w:tr>", text, re.S):
        cells = re.findall(r"<w:tc>.*?</w:tc>", row, re.S)
        rows.append([
            re.sub(r"<[^>]+>", "", cell).strip() for cell in cells
        ])
    return rows


def unicode_candidates(path):
    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml")
    import xml.etree.ElementTree as ET

    root = ET.fromstring(document)
    catalog_path = __file__.rsplit("\\", 1)[0] + "/../scripts/unicode_vertical_catalog.json"
    catalog = json.load(open(catalog_path, encoding="utf-8"))
    entries = catalog["entries"]
    count = 0
    for text_node in root.iter(NS_W + "t"):
        for char in (text_node.text or ""):
            if f"U+{ord(char):04X}" in entries:
                count += 1
    return count


def comment_element(path, comment_id):
    """Byte-exact element of one comment definition (byte-evidence)."""
    with zipfile.ZipFile(path) as archive:
        raw = archive.read("word/comments.xml")
    start = raw.find(f'<w:comment w:id="{comment_id}"'.encode())
    if start < 0:
        return {"error": f"comment {comment_id} not found"}
    end = raw.find(b"</w:comment>", start) + len(b"</w:comment>")
    return {"element": raw[start:end].decode("utf-8")}


KINDS = {
    "revision-inventory": revision_inventory,
    "view-text": view_text,
    "comment-inventory": comment_inventory,
    "comment-element": comment_element,
    "table-cells": table_cells,
    "unicode-candidates": unicode_candidates,
}


def main():
    kind = sys.argv[1]
    path = sys.argv[2]
    extra = sys.argv[3:]
    if kind not in KINDS:
        print(json.dumps({"error": f"unknown kind {kind}"}))
        return 1
    if kind == "view-text":
        result = KINDS[kind](path, extra[0])
    elif kind == "table-cells":
        result = KINDS[kind](path, int(extra[0]) if extra else 0)
    elif kind == "comment-element":
        result = KINDS[kind](path, extra[0])
    else:
        result = KINDS[kind](path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
