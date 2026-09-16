"""Receipt-bound foreign DOCX candidate admission.

This module is deliberately filesystem-shaped and engine-owned: a candidate is
admitted only against the Version named by its receipt. It never chooses a base
by diff size or similarity.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .protocol import semantic_sha256
    from .store import STORE_DIR_NAME
    from .typed_core import (
        AnchorNode,
        InlineNode,
        OpaqueNode,
        RangeNode,
        RevisionNode,
        TextNode,
        parse_typed,
    )
except ImportError:  # pragma: no cover - direct script execution
    from scripts.protocol import semantic_sha256  # type: ignore[no-redef]
    from scripts.store import STORE_DIR_NAME  # type: ignore[no-redef]
    from scripts.typed_core import (  # type: ignore[no-redef]
        AnchorNode,
        InlineNode,
        OpaqueNode,
        RangeNode,
        RevisionNode,
        TextNode,
        parse_typed,
    )

RECEIPT_SCHEMA = "docx2typed-foreign-candidate-1"
LOCATOR_SCHEMA = "docx2typed-foreign-candidate-locator-1"
RECEIPT_SUFFIX = ".foreign-candidate.json"
STORE_RECEIPT_DIR = "foreign-candidates"
_KNOWN_PARTS = {
    "word/document.xml",
    "word/styles.xml",
    "word/numbering.xml",
    "word/settings.xml",
    "word/webSettings.xml",
    "word/fontTable.xml",
    "word/theme/theme1.xml",
}
_PACKAGE_PARTS = {"[Content_Types].xml", "_rels/.rels"}
_OPAQUE_PREFIXES = (
    "customXml/",
    "word/media/",
    "word/embeddings/",
    "word/activeX/",
    "word/ink/",
)
_WORD_XML_RE = re.compile(r"^word/(?:header|footer)\d+\.xml$|^word/(?:comments|footnotes|endnotes)\.xml$")
_CONTENT_TYPES_PART = "[Content_Types].xml"
_DOCUMENT_RELS_PART = "word/_rels/document.xml.rels"
_IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_PNG_SIGNATURE = "89504e470d0a1a0a"
_MEDIA_PART_RE = re.compile(r"^word/media/image\d+\.png$")



class ForeignEditError(ValueError):
    """A fail-closed admission decision with a stable code and details."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = message
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_candidate_id(operation_id: str) -> str:
    """Stable for one operation retry, unique across omitted operation IDs."""
    seed = str(operation_id).strip() or uuid.uuid4().hex
    return "FC" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8].upper()


def receipt_path(candidate: str | Path) -> Path:
    return Path(str(Path(candidate).resolve()) + RECEIPT_SUFFIX)


def normalize_target(target: list[str] | tuple[str, ...] | None) -> list[str]:
    if target is None:
        return []
    if not isinstance(target, (list, tuple)):
        raise ForeignEditError("foreign-target-invalid", "target must be an array of strings")
    values: list[str] = []
    for item in target:
        if not isinstance(item, str):
            raise ForeignEditError("foreign-target-invalid", "target entries must be strings")
        value = item.strip()
        if not value:
            raise ForeignEditError("foreign-target-invalid", "target entries must not be empty")
        if value not in values:
            values.append(value)
    return values


def target_digest(target: list[str]) -> str:
    return semantic_sha256({"schema": "docx2typed-foreign-target-1", "target": target})
def make_receipt(
    *,
    candidate_id: str,
    family_id: str,
    workspace_id: str,
    base_version: str,
    base_commit: str,
    base_tree: str,
    base_export_sha256: str,
    target: list[str],
    anchors: list[dict[str, Any]],
    candidate_original_sha256: str,
    synthetic: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "candidate_id": candidate_id,
        "family_id": family_id,
        "workspace_id": workspace_id,
        "base_version": base_version,
        "base_commit": base_commit,
        "base_tree": base_tree,
        "base_export_sha256": base_export_sha256,
        "target": list(target),
        "target_digest": target_digest(target),
        "anchors": anchors,
        "anchor_set": semantic_sha256(anchors),
        "created_at": str(created_at or now_iso()),
        "candidate_original_sha256": candidate_original_sha256,
        "synthetic": bool(synthetic),
    }


def receipt_bytes(receipt: dict[str, Any]) -> bytes:
    return (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def locator_bytes(candidate_id: str) -> bytes:
    return (
        json.dumps(
            {"schema": LOCATOR_SCHEMA, "candidate_id": str(candidate_id)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def receipt_digest(receipt: dict[str, Any]) -> str:
    return semantic_sha256(receipt)


def store_receipt_path(workdir: str | Path, candidate_id: str) -> Path:
    """The engine-store copy of a receipt: the authoritative record. A file
    sitting next to the candidate lives in attacker-writable space and can be
    moved along with it; this one cannot be widened without mutating the
    engine's own store. The directory is created here because publication is a
    plain atomic rename, which needs an existing destination parent."""
    directory = Path(workdir) / STORE_DIR_NAME / STORE_RECEIPT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{candidate_id}.json"


def load_store_receipt(workdir: str | Path, candidate_id: str) -> dict[str, Any] | None:
    path = store_receipt_path(workdir, candidate_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForeignEditError("foreign-receipt-invalid", f"stored candidate receipt is unreadable: {path}") from exc
    return validate_receipt(data, str(candidate_id))


def load_locator(candidate: str | Path) -> str | None:
    """Read the candidate-side locator. It carries a candidate id and nothing
    else, and it lives in user-writable space beside the file an external tool
    edited — it points at authority, it never is authority."""
    path = receipt_path(candidate)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForeignEditError("foreign-receipt-invalid", f"candidate locator is unreadable: {path}") from exc
    if not isinstance(data, dict) or data.get("schema") != LOCATOR_SCHEMA:
        raise ForeignEditError("foreign-receipt-invalid", f"unsupported candidate locator: {path}")
    candidate_id = str(data.get("candidate_id") or "")
    if not candidate_id:
        raise ForeignEditError("foreign-receipt-invalid", f"candidate locator has no candidate_id: {path}")
    return candidate_id


def validate_receipt(data: Any, candidate_id: str | None = None) -> dict[str, Any]:
    """Structural + self-consistency validation of one receipt document.

    A receipt always comes from an engine store, so this only rejects corruption
    and candidate-id confusion. It is NOT authenticity: the authority of a
    receipt is the store location it was read from, never a digest alone."""
    if not isinstance(data, dict) or data.get("schema") != RECEIPT_SCHEMA:
        raise ForeignEditError("foreign-receipt-invalid", "unsupported candidate receipt")
    required = (
        "candidate_id", "family_id", "workspace_id", "base_version", "base_commit",
        "base_tree", "base_export_sha256", "target", "target_digest", "anchor_set",
        "candidate_original_sha256",
    )
    missing = [name for name in required if not data.get(name)]
    if missing:
        raise ForeignEditError("foreign-receipt-invalid", f"receipt is missing: {', '.join(missing)}")
    if candidate_id and str(data.get("candidate_id")) != str(candidate_id):
        raise ForeignEditError(
            "foreign-candidate-id-mismatch",
            f"candidate_id {candidate_id!r} does not match receipt {data.get('candidate_id')!r}",
        )
    target = normalize_target(data.get("target"))
    if str(data.get("target_digest")) != target_digest(target):
        raise ForeignEditError("foreign-receipt-invalid", "receipt target digest does not match target")
    anchors = data.get("anchors")
    if not isinstance(anchors, list) or str(data.get("anchor_set")) != semantic_sha256(anchors):
        raise ForeignEditError("foreign-receipt-invalid", "receipt anchor set is not self-consistent")
    for name in ("base_export_sha256", "candidate_original_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(data.get(name))):
            raise ForeignEditError("foreign-receipt-invalid", f"receipt field {name} is not a SHA-256")
    return data

def _styleless_skeleton(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    if not value:
        return []
    if isinstance(value[0], list):
        return [_styleless_skeleton(item) for item in value]
    kind = value[0]
    if kind == "text":
        return ["text"]
    if kind == "inline":
        return ["inline", value[1], value[3] if len(value) > 3 else []]
    if kind in {"range", "revision"}:
        return [kind, value[1], value[2], _styleless_skeleton(value[3]) if len(value) > 3 else []]
    if kind == "opaque":
        return ["opaque", value[1], value[2] if len(value) > 2 else []]
    return [_styleless_skeleton(item) for item in value]


def _anchor_key(record: dict[str, Any]) -> str:
    return semantic_sha256(
        {
            "part": str(record.get("part_key") or ""),
            "entry": str(record.get("part_entry_id") or ""),
            "structure": _styleless_skeleton(record.get("skeleton") or []),
            "section": bool(record.get("section_bearing")),
            "editable": bool(record.get("editable", True)),
            "mark": (record.get("mark_revision") or {}).get("kind") if isinstance(record.get("mark_revision"), dict) else None,
        }
    )


def anchor_set(workdir: str | Path) -> tuple[list[dict[str, Any]], str]:
    try:
        data = json.loads((Path(workdir) / "format.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForeignEditError("foreign-workdir-invalid", f"format.json is unreadable: {workdir}") from exc
    records = data.get("paragraphs") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise ForeignEditError("foreign-workdir-invalid", "format.json has no paragraph records")
    anchors: list[dict[str, Any]] = []
    ids: set[str] = set()
    external_ids: set[str] = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ForeignEditError("foreign-workdir-invalid", f"paragraph record {index} has no id")
        paragraph_id = str(raw["id"])
        if paragraph_id in ids:
            raise ForeignEditError("foreign-identity-ambiguous", f"duplicate paragraph identity: {paragraph_id}")
        ids.add(paragraph_id)
        external_id = str(raw.get("external_id") or "")
        if external_id and external_id in external_ids:
            raise ForeignEditError("foreign-identity-ambiguous", f"duplicate external paragraph identity: {external_id}")
        if external_id:
            external_ids.add(external_id)
        anchors.append(
            {
                "id": paragraph_id,
                "external_id": external_id,
                "part": str(raw.get("part_key") or ""),
                "entry": str(raw.get("part_entry_id") or ""),
                "location": int(raw.get("original_index", index)),
                "structure": _styleless_skeleton(raw.get("skeleton") or []),
                "structural_key": _anchor_key(raw),
                "section_bearing": bool(raw.get("section_bearing")),
                "previous": str(records[index - 1].get("id")) if index and isinstance(records[index - 1], dict) else None,
                "next": str(records[index + 1].get("id")) if index + 1 < len(records) and isinstance(records[index + 1], dict) else None,
            }
        )
    return anchors, semantic_sha256(anchors)


def _attrs(attrs: dict[str, str]) -> list[list[str]]:
    return [[str(key), str(value)] for key, value in sorted(attrs.items())]


def _node_signature(node: Any, *, include_style: bool) -> Any:
    if isinstance(node, TextNode):
        # vertical alignment is its own dimension of the text, not part of the
        # style id: a candidate that only strips superscript off "Ca^{2+}" must
        # read as a change, never as an equal snapshot. getattr keeps this
        # correct on a base whose TextNode predates the field.
        return [
            "text",
            node.text,
            getattr(node, "vertical", None),
            node.style_id if include_style else None,
        ]
    if isinstance(node, AnchorNode):
        return ["anchor", node.kind, _attrs(node.attrs)]
    if isinstance(node, InlineNode):
        return ["inline", node.kind, node.style_id if include_style else None, _attrs(node.attrs)]
    if isinstance(node, OpaqueNode):
        return ["opaque", node.kind, _attrs(node.attrs)]
    if isinstance(node, (RangeNode, RevisionNode)):
        return [
            "range" if isinstance(node, RangeNode) else "revision",
            node.kind,
            _attrs(node.attrs),
            [_node_signature(child, include_style=include_style) for child in node.children],
        ]
    raise TypeError(f"unsupported typed node: {type(node).__name__}")


def _opaque_nodes(nodes: list[Any]) -> list[Any]:
    values: list[Any] = []
    for node in nodes:
        if isinstance(node, OpaqueNode):
            values.append(_node_signature(node, include_style=False))
        elif isinstance(node, (RangeNode, RevisionNode)):
            values.extend(_opaque_nodes(node.children))
    return values


def typed_snapshot(workdir: str | Path) -> dict[str, Any]:
    root = Path(workdir)
    try:
        document = parse_typed((root / "typed.md").read_text(encoding="utf-8"))
        format_data = json.loads((root / "format.json").read_text(encoding="utf-8"))
        styles_data = json.loads((root / "styles.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ForeignEditError("foreign-workdir-invalid", f"candidate workdir is unreadable: {root}") from exc
    records = {
        str(item.get("id")): item
        for item in (format_data.get("paragraphs") or [])
        if isinstance(item, dict) and item.get("id")
    }
    paragraphs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, paragraph in enumerate(document.paragraphs):
        paragraph_id = str(paragraph.paragraph_id)
        if paragraph_id in seen:
            raise ForeignEditError("foreign-identity-ambiguous", f"duplicate typed paragraph: {paragraph_id}")
        seen.add(paragraph_id)
        record = records.get(paragraph_id, {})
        mark = paragraph.mark_revision or {}
        mark_signature = [str(mark.get("kind")), _attrs(mark.get("attrs") or {})] if mark else None
        external_id = str(record.get("external_id") or "")
        paragraphs.append(
            {
                "id": paragraph_id,
                "index": index,
                "external_id": external_id,
                "part": str(record.get("part_key") or paragraph.part_key or ""),
                "entry": str(record.get("part_entry_id") or paragraph.part_entry_id or ""),
                "structural_key": _anchor_key(record) if record else semantic_sha256({"part": paragraph.part_key, "index": index}),
                "semantic": {
                    "part": str(record.get("part_key") or paragraph.part_key or ""),
                    "entry": str(record.get("part_entry_id") or paragraph.part_entry_id or ""),
                    "section": bool(record.get("section_bearing", paragraph.section_bearing)),
                    "content": [_node_signature(node, include_style=False) for node in paragraph.nodes],
                    "mark": mark_signature,
                },
                "format": {
                    "base_style": str(record.get("base_style") or paragraph.base_style or ""),
                    "insertion_style": str(record.get("insertion_style") or ""),
                    "content": [_node_signature(node, include_style=True) for node in paragraph.nodes],
                    "mark": mark_signature,
                },
                "opaque": _opaque_nodes(paragraph.nodes),
            }
        )
        if not external_id:
            paragraphs[-1].pop("external_id", None)
    styles = {
        str(key): {
            "canonical": str(value.get("canonical") or ""),
            "features": value.get("features") or {},
        }
        for key, value in (styles_data.get("styles") or {}).items()
        if isinstance(value, dict)
    }
    tokens = {
        str(key): hashlib.sha256(str(value.get("raw") or "").encode("utf-8")).hexdigest()
        for key, value in (format_data.get("tokens") or {}).items()
        if isinstance(value, dict) and value.get("kind") in {"opaque", "unsupported-run"}
    }
    return {
        "paragraphs": paragraphs,
        "by_id": {item["id"]: item for item in paragraphs},
        "deletions": sorted(str(item) for item in (document.deletions or [])),
        "styles": styles,
        "opaque_tokens": tokens,
    }


def _unique_map(items: list[dict[str, Any]], field: str) -> dict[str, int]:
    positions: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        value = str(item.get(field) or "")
        if value:
            positions.setdefault(value, []).append(index)
    return {value: indexes[0] for value, indexes in positions.items() if len(indexes) == 1}


def _duplicate_values(items: list[dict[str, Any]], field: str) -> set[str]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(field) or "")
        if value:
            counts[value] = counts.get(value, 0) + 1
    return {value for value, count in counts.items() if count > 1}


def align_paragraphs(base: dict[str, Any], candidate: dict[str, Any], expected_anchors: list[dict[str, Any]]) -> dict[str, Any]:
    """Map candidate paragraphs without a similarity threshold or base guess.

    Every pairing records how it was proven. ``strong`` means the evidence
    identifies the SAME paragraph — a stable external id, an unchanged id
    universe, a unique structural identity, or byte-identical content.
    ``order`` means only document position lined the leftovers up: enough to
    compare two paragraphs, but NOT proof of which base paragraph a candidate
    paragraph is. Scope authorization may rely only on ``strong`` proof; the
    change report may use both.
    """
    base_items, candidate_items = base["paragraphs"], candidate["paragraphs"]
    base_anchors = {str(item["id"]): item for item in expected_anchors}
    if set(base_anchors) != {item["id"] for item in base_items}:
        raise ForeignEditError("foreign-receipt-invalid", "receipt anchors do not match the pinned base")
    candidate_anchors = anchor_set_from_snapshot(candidate)
    mapping: dict[int, int] = {}
    used_candidate: set[int] = set()
    ambiguous_external = sorted(
        _duplicate_values(expected_anchors, "external_id")
        & _duplicate_values(candidate_anchors, "external_id")
    )
    if ambiguous_external:
        raise ForeignEditError(
            "foreign-identity-ambiguous",
            "candidate paragraphs have repeated external identities",
            {"external_ids": ambiguous_external},
        )
    methods: list[dict[str, Any]] = []


    # Stable external IDs are the strongest identity. Duplicate IDs are never
    # resolved by order.
    base_external = _unique_map(expected_anchors, "external_id")
    candidate_external = _unique_map(candidate_anchors, "external_id")
    for external_id, base_index in base_external.items():
        candidate_index = candidate_external.get(external_id)
        if candidate_index is not None:
            mapping[base_index] = candidate_index
            used_candidate.add(candidate_index)
            methods.append({"method": "external-id", "proof": "strong", "base": base_items[base_index]["id"], "candidate": candidate_items[candidate_index]["id"]})
    # Positional paragraph IDs are useful only when the entire ID universe is
    # unchanged. A scratch re-extract may renumber after an insertion, so a
    # partial ID overlap is not evidence of identity.
    if (
        len(base_items) == len(candidate_items)
        and {str(item["id"]) for item in expected_anchors} == {str(item["id"]) for item in candidate_anchors}
    ):
        base_ids = _unique_map(expected_anchors, "id")
        candidate_ids = _unique_map(candidate_anchors, "id")
        for paragraph_id, base_index in base_ids.items():
            candidate_index = candidate_ids.get(paragraph_id)
            if candidate_index is None:
                continue
            mapping[base_index] = candidate_index
            used_candidate.add(candidate_index)
            methods.append(
                {
                    "method": "paragraph-id",
                    "proof": "strong",
                    "base": base_items[base_index]["id"],
                    "candidate": candidate_items[candidate_index]["id"],
                }
            )
    base_key_positions = _unique_map(expected_anchors, "structural_key")
    candidate_key_positions = _unique_map(candidate_anchors, "structural_key")
    for key, base_index in base_key_positions.items():
        candidate_index = candidate_key_positions.get(key)
        if candidate_index is None or base_index in mapping or candidate_index in used_candidate:
            continue
        mapping[base_index] = candidate_index
        used_candidate.add(candidate_index)
        methods.append({"method": "structural", "proof": "strong", "base": base_items[base_index]["id"], "candidate": candidate_items[candidate_index]["id"]})

    # A plain paragraph shares its structural key with every other plain
    # paragraph of the same style, so identity alone cannot tell them apart.
    # Byte-identical visible content is exact evidence, and it is what survives
    # a scratch re-extract renumbering P0..Pn after an insertion.
    def _content_key(item: dict[str, Any]) -> str:
        return semantic_sha256(item["semantic"])

    base_content: dict[str, list[int]] = {}
    for index, item in enumerate(base_items):
        base_content.setdefault(_content_key(item), []).append(index)
    candidate_content: dict[str, list[int]] = {}
    for index, item in enumerate(candidate_items):
        candidate_content.setdefault(_content_key(item), []).append(index)
    for key, base_indexes in base_content.items():
        candidate_indexes = candidate_content.get(key) or []
        if len(base_indexes) != 1 or len(candidate_indexes) != 1:
            continue
        base_index, candidate_index = base_indexes[0], candidate_indexes[0]
        if base_index in mapping or candidate_index in used_candidate:
            continue
        mapping[base_index] = candidate_index
        used_candidate.add(candidate_index)
        methods.append(
            {
                "method": "exact-content",
                "proof": "strong",
                "base": base_items[base_index]["id"],
                "candidate": candidate_items[candidate_index]["id"],
            }
        )

    # Nothing stronger applies to what identity could not pin. Pair the
    # leftovers in document order: deterministic sequence alignment, not a
    # similarity score. It cannot hide a change — paired paragraphs are
    # compared and unpaired ones are reported as removed/added, so an
    # out-of-target edit still refuses on its own attribution.
    leftover_base = [index for index in range(len(base_items)) if index not in mapping]
    leftover_candidate = [index for index in range(len(candidate_items)) if index not in used_candidate]
    for base_index, candidate_index in zip(leftover_base, leftover_candidate):
        mapping[base_index] = candidate_index
        used_candidate.add(candidate_index)
        methods.append(
            {
                "method": "order",
                "proof": "order",
                "base": base_items[base_index]["id"],
                "candidate": candidate_items[candidate_index]["id"],
            }
        )


    unmatched_base = [index for index in range(len(base_items)) if index not in mapping]
    unmatched_candidate = [index for index in range(len(candidate_items)) if index not in used_candidate]
    if not mapping and len(base_items) == len(candidate_items) == 1:
        mapping[0] = 0
        unmatched_base, unmatched_candidate = [], []
        methods.append({"method": "single-item-sequence", "proof": "strong", "base": base_items[0]["id"], "candidate": candidate_items[0]["id"]})
    return {
        "base_to_candidate": mapping,
        "unmatched_base": unmatched_base,
        "unmatched_candidate": unmatched_candidate,
        "methods": methods,
    }


def alignment_proof(alignment: dict[str, Any], base_ids: list[str]) -> dict[str, str]:
    """{base paragraph id -> proof strength} from the pairing methods."""
    by_base = {
        str(item["base"]): str(item.get("proof") or "strong")
        for item in alignment["methods"]
    }
    return {paragraph_id: by_base.get(paragraph_id, "unpaired") for paragraph_id in base_ids}


def _anchored_bracket(
    base_items: list[dict[str, Any]],
    candidate_items: list[dict[str, Any]],
    candidate_index: int,
    proof: dict[str, str] | None = None,
) -> bool:
    """True when an inserted paragraph's position is bracketed by proof.

    An insertion location may only be reported by strong evidence on BOTH
    sides: the paragraph before it and the paragraph after it in the candidate
    must each be a strongly-identified base paragraph, and those two base
    paragraphs must be adjacent. Otherwise the location is a claim about where
    the new paragraph went that no evidence supports."""
    if candidate_index < 0 or candidate_index >= len(candidate_items):
        return False
    base_position = {
        str(item["id"]): index for index, item in enumerate(base_items)
    }
    neighbours: list[int] = []
    for offset in (-1, 1):
        position = candidate_index + offset
        if position < 0 or position >= len(candidate_items):
            return False
        candidate_id = str(candidate_items[position]["id"])
        index = base_position.get(candidate_id)
        if index is None:
            return False
        if proof is not None and proof.get(candidate_id) != "strong":
            return False
        neighbours.append(index)
    return abs(neighbours[0] - neighbours[1]) == 1


def anchor_set_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(item["id"]),
            "external_id": str(item.get("external_id") or ""),
            "part": str(item.get("part") or ""),
            "entry": str(item.get("entry") or ""),
            "location": int(item.get("index", index)),
            "structure": [],
            "structural_key": str(item.get("structural_key") or ""),
        }
        for index, item in enumerate(snapshot["paragraphs"])
    ]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _xml_semantics(value: bytes) -> Any:
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return None

    def walk(element: ET.Element) -> Any:
        attrs = sorted(
            (str(key), str(val))
            for key, val in element.attrib.items()
            if not key.rsplit("}", 1)[-1].startswith("rsid")
        )
        return [str(element.tag), attrs, element.text or "", [walk(child) for child in list(element)]]

    return walk(root)

def _drawing_records(value: bytes) -> list[dict[str, Any]] | None:
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return None
    records: list[dict[str, Any]] = []
    paragraphs = [
        element
        for element in root.iter()
        if str(element.tag).rsplit("}", 1)[-1] == "p"
    ]
    for paragraph_index, paragraph in enumerate(paragraphs):
        for drawing in paragraph.iter():
            if str(drawing.tag).rsplit("}", 1)[-1] != "drawing":
                continue
            embeds = sorted(
                str(attribute_value)
                for blip in drawing.iter()
                if str(blip.tag).rsplit("}", 1)[-1] == "blip"
                for attribute_name, attribute_value in blip.attrib.items()
                if str(attribute_name).rsplit("}", 1)[-1] == "embed"
            )
            links = sorted(
                str(attribute_value)
                for blip in drawing.iter()
                if str(blip.tag).rsplit("}", 1)[-1] == "blip"
                for attribute_name, attribute_value in blip.attrib.items()
                if str(attribute_name).rsplit("}", 1)[-1] == "link"
            )
            records.append(
                {
                    "paragraph_index": paragraph_index,
                    "embeds": embeds,
                    "links": links,
                    "signature": semantic_sha256(
                        _xml_semantics(ET.tostring(drawing, encoding="utf-8")) or []
                    ),
                }
            )
    return records

def _document_without_drawing(value: bytes, signature: str) -> Any:
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return None

    def visit(parent: ET.Element) -> bool:
        for child in list(parent):
            if str(child.tag).rsplit("}", 1)[-1] == "drawing":
                child_signature = semantic_sha256(
                    _xml_semantics(ET.tostring(child, encoding="utf-8")) or []
                )
                if child_signature == signature:
                    parent.remove(child)
                    return True
            if visit(child):
                if str(child.tag).rsplit("}", 1)[-1] == "r" and not list(child):
                    parent.remove(child)
                return True
        return False

    if not visit(root):
        return None
    return _xml_semantics(ET.tostring(root, encoding="utf-8"))


def _xml_without_child_attributes(
    value: bytes, tag: str, attributes: dict[str, str]
) -> Any:
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return None

    def visit(parent: ET.Element) -> bool:
        for child in list(parent):
            if (
                str(child.tag).rsplit("}", 1)[-1] == tag
                and dict(child.attrib) == attributes
            ):
                parent.remove(child)
                return True
            if visit(child):
                return True
        return False

    if not visit(root):
        return None
    return _xml_semantics(ET.tostring(root, encoding="utf-8"))

def _multiset_delta(before: list[Any], after: list[Any]) -> tuple[list[Any], list[Any]]:
    remaining = list(before)
    added: list[Any] = []
    for item in after:
        try:
            remaining.remove(item)
        except ValueError:
            added.append(item)
    return added, remaining


def _only_new_drawing(before: list[Any], after: list[Any]) -> bool:
    added, removed = _multiset_delta(before, after)
    if removed or len(added) != 1:
        return False
    value = added[0]
    if not isinstance(value, list) or len(value) < 3 or value[0] != "opaque":
        return False
    attributes = dict(value[2]) if isinstance(value[2], list) else {}
    return attributes.get("tag") == "w:drawing"


def _media_addition_proof(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_parts = base["parts"]
    candidate_parts = candidate["parts"]
    added = sorted(set(candidate_parts) - set(base_parts))
    removed = sorted(set(base_parts) - set(candidate_parts))
    changed = sorted(
        name
        for name in set(base_parts) & set(candidate_parts)
        if base_parts[name] != candidate_parts[name]
    )
    media_parts = [name for name in added if _MEDIA_PART_RE.fullmatch(name)]
    if removed or added != media_parts or len(media_parts) != 1:
        return {"allowed": False, "reason": "media inventory is not one new PNG"}
    if set(changed) != {_CONTENT_TYPES_PART, _DOCUMENT_RELS_PART, "word/document.xml"}:
        return {"allowed": False, "reason": "media addition changed an unexpected part"}
    media_part = media_parts[0]
    if candidate.get("media_headers", {}).get(media_part) != _PNG_SIGNATURE:
        return {"allowed": False, "reason": "new media part is not a PNG"}

    def relationship_map(records: Any) -> dict[str, dict[str, str]] | None:
        if not isinstance(records, list):
            return None
        result: dict[str, dict[str, str]] = {}
        for record in records:
            if not isinstance(record, list):
                return None
            attributes: dict[str, str] = {}
            for pair in record:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    return None
                attributes[str(pair[0])] = str(pair[1])
            relationship_id = attributes.get("Id")
            if not relationship_id or relationship_id in result:
                return None
            result[relationship_id] = attributes
        return result

    base_relationships = relationship_map(
        base["relationships"].get(_DOCUMENT_RELS_PART)
    )
    candidate_relationships = relationship_map(
        candidate["relationships"].get(_DOCUMENT_RELS_PART)
    )
    if base_relationships is None or candidate_relationships is None:
        return {"allowed": False, "reason": "document relationships are unreadable"}
    if set(base_relationships) - set(candidate_relationships):
        return {"allowed": False, "reason": "an existing relationship was removed"}
    if any(
        base_relationships[relationship_id] != candidate_relationships[relationship_id]
        for relationship_id in base_relationships
    ):
        return {"allowed": False, "reason": "an existing relationship was changed"}
    new_relationships = [
        attributes
        for relationship_id, attributes in candidate_relationships.items()
        if relationship_id not in base_relationships
    ]
    if len(new_relationships) != 1:
        return {"allowed": False, "reason": "media addition needs one new relationship"}
    relationship = new_relationships[0]
    if (
        set(relationship) != {"Id", "Target", "Type"}
        or not re.fullmatch(r"rId\d+", relationship.get("Id", ""))
        or relationship.get("Type") != _IMAGE_REL_TYPE
        or relationship.get("Target") != f"media/{media_part.rsplit('/', 1)[-1]}"
    ):
        return {"allowed": False, "reason": "new relationship does not own the new PNG"}
    stripped_relationships = _xml_without_child_attributes(
        candidate["package_xml"].get(_DOCUMENT_RELS_PART, b""),
        "Relationship",
        relationship,
    )
    if (
        stripped_relationships is None
        or stripped_relationships
        != base["package_semantics"].get(_DOCUMENT_RELS_PART)
    ):
        return {"allowed": False, "reason": "relationship XML changed beyond the new image link"}

    base_content_types, candidate_content_types = (
        base.get("content_types"),
        candidate.get("content_types"),
    )
    if not isinstance(base_content_types, list) or not isinstance(candidate_content_types, list):
        return {"allowed": False, "reason": "content types are unreadable"}
    added_content_types, removed_content_types = _multiset_delta(
        base_content_types, candidate_content_types
    )
    if removed_content_types or len(added_content_types) != 1:
        return {"allowed": False, "reason": "content types changed beyond one PNG default"}
    content_type = added_content_types[0]
    if not isinstance(content_type, list) or len(content_type) != 2 or content_type[0] != "Default":
        return {"allowed": False, "reason": "new content type is not a default"}
    content_attributes = dict(content_type[1]) if isinstance(content_type[1], list) else {}
    if content_attributes != {"ContentType": "image/png", "Extension": "png"}:
        return {"allowed": False, "reason": "new content type is not image/png"}
    stripped_content_types = _xml_without_child_attributes(
        candidate["package_xml"].get(_CONTENT_TYPES_PART, b""),
        "Default",
        content_attributes,
    )
    if (
        stripped_content_types is None
        or stripped_content_types
        != base["package_semantics"].get(_CONTENT_TYPES_PART)
    ):
        return {"allowed": False, "reason": "content-type XML changed beyond the new PNG default"}

    base_drawings, candidate_drawings = base.get("drawings"), candidate.get("drawings")
    if not isinstance(base_drawings, list) or not isinstance(candidate_drawings, list):
        return {"allowed": False, "reason": "document drawings are unreadable"}
    added_drawings, removed_drawings = _multiset_delta(base_drawings, candidate_drawings)
    if removed_drawings or len(added_drawings) != 1:
        return {"allowed": False, "reason": "media addition needs one new drawing"}
    drawing = added_drawings[0]
    if drawing.get("links") or drawing.get("embeds") != [relationship["Id"]]:
        return {"allowed": False, "reason": "new drawing does not reference only the new relationship"}
    stripped_document = _document_without_drawing(
        candidate.get("document_xml", b""), drawing["signature"]
    )
    if stripped_document is None or stripped_document != base["xml_semantics"].get("word/document.xml"):
        return {"allowed": False, "reason": "document changed beyond the new drawing"}
    return {
        "allowed": True,
        "media_part": media_part,
        "relationship_id": relationship["Id"],
        "drawing": drawing,
    }



def _style_definitions(value: bytes) -> dict[str, str]:
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return {}
    definitions: dict[str, str] = {}
    for child in list(root):
        if str(child.tag).rsplit("}", 1)[-1] != "style":
            continue
        style_id = next(
            (
                str(attribute_value)
                for attribute_name, attribute_value in child.attrib.items()
                if str(attribute_name).rsplit("}", 1)[-1] == "styleId"
            ),
            "",
        )
        if style_id:
            definitions[style_id] = semantic_sha256(
                _xml_semantics(ET.tostring(child, encoding="utf-8")) or []
            )
    return definitions


def _document_semantics(value: bytes) -> dict[str, Any]:
    """One parse yielding the document signature with every ``w:sectPr``
    replaced by a placeholder, plus each sectPr signature in document order.

    Page setup lives only inside sectPr containers, so a body signature that
    still matches while the section list differs proves the delta is sections
    and nothing else. Anything else stays unattributed."""
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return {"body": None, "sections": []}
    sections: list[Any] = []

    def walk(element: ET.Element) -> Any:
        if str(element.tag).rsplit("}", 1)[-1] == "sectPr":
            sections.append(_xml_semantics(ET.tostring(element, encoding="utf-8")) or [])
            return None
        attrs = sorted(
            (str(key), str(val))
            for key, val in element.attrib.items()
            if not key.rsplit("}", 1)[-1].startswith("rsid")
        )
        children = []
        for child in list(element):
            value = walk(child)
            if value is not None:
                children.append(value)
        return [str(element.tag), attrs, element.text or "", children]

    return {"body": walk(root), "sections": [semantic_sha256(item) for item in sections]}


def _part_kind(name: str) -> str:
    if name in _PACKAGE_PARTS or name.endswith(".rels"):
        return "package"
    if name in _KNOWN_PARTS or _WORD_XML_RE.match(name):
        return "structured"
    if name.startswith(_OPAQUE_PREFIXES) or name.startswith("word/"):
        return "opaque"
    return "opaque"


def package_snapshot(docx: str | Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(docx) as archive:
            names = sorted(archive.namelist())
            if len(names) != len(set(names)):
                raise ForeignEditError("foreign-package-invalid", "DOCX contains duplicate package entries")
            raw = {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise ForeignEditError("foreign-package-invalid", f"not a valid DOCX: {docx}") from exc
    relationships: dict[str, Any] = {}
    content_types: Any = []
    for name, value in raw.items():
        if name.endswith(".rels"):
            try:
                root = ET.fromstring(value)
                relationships[name] = sorted(
                    [
                        sorted((str(key), str(val)) for key, val in child.attrib.items())
                        for child in root
                        if str(child.tag).rsplit("}", 1)[-1] == "Relationship"
                    ]
                )
            except ET.ParseError:
                relationships[name] = None
        elif name == "[Content_Types].xml":
            try:
                root = ET.fromstring(value)
                content_types = sorted(
                    [
                        [
                            str(child.tag).rsplit("}", 1)[-1],
                            sorted((str(key), str(val)) for key, val in child.attrib.items()),
                        ]
                        for child in root
                    ]
                )
            except ET.ParseError:
                content_types = None
    return {
        "parts": {name: _sha256_bytes(value) for name, value in raw.items()},
        "xml_semantics": {
            name: _xml_semantics(value)
            for name, value in raw.items()
            if name.endswith(".xml")
        },
        "relationships": relationships,
        "package_semantics": {
            name: _xml_semantics(value)
            for name, value in raw.items()
            if name.endswith(".rels") or name == _CONTENT_TYPES_PART
        },
        "package_xml": {
            name: raw[name]
            for name in (_CONTENT_TYPES_PART, _DOCUMENT_RELS_PART)
            if name in raw
        },
        "content_types": content_types,
        "style_definitions": _style_definitions(raw.get("word/styles.xml", b"")),
        "document": _document_semantics(raw.get("word/document.xml", b"")),
        "document_xml": raw.get("word/document.xml", b""),
        "drawings": _drawing_records(raw.get("word/document.xml", b"")),
        "media_headers": {
            name: value[:8].hex()
            for name, value in raw.items()
            if name.startswith("word/media/")
        },
    }



def package_diff(base_docx: str | Path, candidate_docx: str | Path) -> dict[str, Any]:
    base, candidate = package_snapshot(base_docx), package_snapshot(candidate_docx)
    base_names, candidate_names = set(base["parts"]), set(candidate["parts"])
    added = sorted(candidate_names - base_names)
    removed = sorted(base_names - candidate_names)
    changed = sorted(name for name in base_names & candidate_names if base["parts"][name] != candidate["parts"][name])
    base_styles = base["style_definitions"]
    candidate_styles = candidate["style_definitions"]
    style_definition_changes = sorted(
        style_id
        for style_id in set(base_styles) | set(candidate_styles)
        if base_styles.get(style_id) != candidate_styles.get(style_id)
    )
    normalization = sorted(
        name
        for name in changed
        if _part_kind(name) == "structured"
        and base["xml_semantics"].get(name) is not None
        and base["xml_semantics"].get(name) == candidate["xml_semantics"].get(name)
    )
    opaque_changed = sorted(
        name for name in changed if _part_kind(name) == "opaque"
    ) + added + removed
    package_changed = sorted(
        name for name in changed if _part_kind(name) == "package"
    )
    inventory_changed = bool(
        added
        or removed
        or package_changed
        or base["relationships"] != candidate["relationships"]
        or base["content_types"] != candidate["content_types"]
    )
    base_sections = base["document"]["sections"]
    candidate_sections = candidate["document"]["sections"]
    sections_changed = bool(
        "word/document.xml" in changed and base_sections != candidate_sections
    )
    # A page-setup delta is cleanly attributable only when the body outside the
    # sectPr containers is provably identical; otherwise something the typed
    # model may not have seen also moved, and the part stays unattributed.
    section_only = bool(
        sections_changed
        and len(base_sections) == len(candidate_sections)
        and base["document"]["body"] is not None
        and base["document"]["body"] == candidate["document"]["body"]
    )
    section_indices = [
        index
        for index in range(max(len(base_sections), len(candidate_sections)))
        if (base_sections[index] if index < len(base_sections) else None)
        != (candidate_sections[index] if index < len(candidate_sections) else None)
    ]
    media_add = _media_addition_proof(base, candidate)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "normalization": normalization,
        "opaque_changed": sorted(set(opaque_changed)),
        "package_changed": package_changed,
        "inventory_changed": inventory_changed,
        "style_definition_changes": style_definition_changes,
        "section_only": section_only,
        "section_indices": section_indices,
        "structured_semantic_changed": sorted(
            name for name in changed
            if _part_kind(name) == "structured" and name not in normalization
        ),
        "part_hashes": {"base": base["parts"], "candidate": candidate["parts"]},
        "media_add": media_add,
    }


def _target_matches(target: list[str], change: dict[str, Any], base: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if not target:
        return True
    paragraph_id = str(change.get("paragraph_id") or "")
    for item in target:
        if item == paragraph_id or item == f"paragraph:{paragraph_id}":
            return True
        if item.startswith("table:") and paragraph_id.startswith(item[6:] + "."):
            return True
        if item.startswith("style:") and change.get("kind") == "dependency":
            changed_styles = {
                str(value) for value in (change.get("styles") or [])
            }
            if item[6:] in changed_styles:
                return True
        # A media-add is scoped by its owning paragraph; all other drawing
        # changes remain opaque casualties and are refused by the package gate.
        if item.startswith("section:") and change.get("kind") == "section":
            # an ordinal target must name THIS section; a bare "section:" taken
            # whole-document would let one confirmed section authorize the rest
            if item[8:] == str(change.get("section")):
                return True
        match = re.fullmatch(r"paragraph:([^.]*)\.\.([^.]*)", item)
        if match and paragraph_id:
            ids = [str(value["id"]) for value in base["paragraphs"]]
            candidate_ids = [str(value["id"]) for value in candidate["paragraphs"]]
            wanted = set(ids)
            wanted.update(candidate_ids)
            if paragraph_id in wanted:
                start, end = match.group(1), match.group(2)
                positions = [values.index(paragraph_id) for values in (ids, candidate_ids) if paragraph_id in values]
                boundaries = [values.index(marker) for values in (ids, candidate_ids) for marker in (start, end) if marker in values]
                if positions and len(boundaries) >= 2 and min(boundaries) <= positions[0] <= max(boundaries):
                    return True
    return False


def analyze_candidate(
    base_workdir: str | Path,
    candidate_workdir: str | Path,
    base_docx: str | Path,
    candidate_docx: str | Path,
    receipt: dict[str, Any],
    target: list[str],
) -> dict[str, Any]:
    expected_anchors = receipt.get("anchors") or []
    computed_anchors, computed_digest = anchor_set(base_workdir)
    if str(receipt.get("anchor_set")) != computed_digest or expected_anchors != computed_anchors:
        raise ForeignEditError("foreign-receipt-stale", "receipt anchors no longer match the pinned base")
    base_snapshot = typed_snapshot(base_workdir)
    candidate_snapshot = typed_snapshot(candidate_workdir)
    alignment = align_paragraphs(base_snapshot, candidate_snapshot, expected_anchors)
    changes: list[dict[str, Any]] = []
    for base_index, candidate_index in sorted(alignment["base_to_candidate"].items()):
        before, after = base_snapshot["paragraphs"][base_index], candidate_snapshot["paragraphs"][candidate_index]
        if before["semantic"] != after["semantic"]:
            changes.append({"kind": "semantic", "paragraph_id": before["id"], "candidate_id": after["id"], "part": before["part"]})
        elif before["format"] != after["format"]:
            changes.append({"kind": "format", "paragraph_id": before["id"], "candidate_id": after["id"], "part": before["part"]})
        if before["opaque"] != after["opaque"]:
            changes.append({"kind": "opaque", "paragraph_id": before["id"], "candidate_id": after["id"], "part": before["part"]})
    for index in alignment["unmatched_candidate"]:
        item = candidate_snapshot["paragraphs"][index]
        changes.append({"kind": "semantic", "paragraph_id": item["id"], "candidate_id": item["id"], "part": item["part"], "state": "added"})
    for index in alignment["unmatched_base"]:
        item = base_snapshot["paragraphs"][index]
        changes.append({"kind": "semantic", "paragraph_id": item["id"], "part": item["part"], "state": "removed"})
    if base_snapshot["deletions"] != candidate_snapshot["deletions"]:
        changes.append({"kind": "semantic", "paragraph_id": "<deletions>", "part": "document"})
    style_changes = sorted(
        key
        for key in set(base_snapshot["styles"]) | set(candidate_snapshot["styles"])
        if base_snapshot["styles"].get(key) != candidate_snapshot["styles"].get(key)
    )
    package = package_diff(base_docx, candidate_docx)
    if package["media_add"]["allowed"]:
        media_changes = []
        for item in changes:
            if item.get("kind") != "opaque":
                continue
            before = base_snapshot["by_id"].get(str(item.get("paragraph_id")))
            after = candidate_snapshot["by_id"].get(str(item.get("candidate_id")))
            if before is not None and after is not None and _only_new_drawing(
                before["opaque"], after["opaque"]
            ):
                media_changes.append(item)
        if len(media_changes) == 1:
            media_changes[0]["kind"] = "media-add"
        else:
            package["media_add"]["allowed"] = False
    if "word/styles.xml" in package["changed"]:
        style_dependencies = sorted(
            set(style_changes) | set(package["style_definition_changes"])
        )
        if style_dependencies:
            changes.append(
                {
                    "kind": "dependency",
                    "paragraph_id": "<styles>",
                    "part": "word/styles.xml",
                    "styles": style_dependencies,
                }
            )
    for part in package["structured_semantic_changed"]:
        if part == "word/document.xml" and any(
            item.get("part") in {"", "document"} for item in changes
        ):
            continue
        if part == "word/styles.xml" and any(
            item.get("kind") == "dependency"
            and item.get("part") == "word/styles.xml"
            for item in changes
        ):
            continue
        if part == "word/document.xml" and any(
            item.get("kind") in {"semantic", "format", "opaque", "media-add", "dependency"}
            for item in changes
        ):
            continue
        # section_only means the body outside sectPr is provably identical, so
        # every document.xml delta is accounted for by the section changes below
        if part == "word/document.xml" and package["section_only"]:
            continue
        changes.append({"kind": "package", "paragraph_id": f"<part:{part}>", "part": part})
    for index in package["section_indices"]:
        # Page setup lives only in sectPr; each differing container is its own
        # deltas so it must still land inside the target, whether or not the
        # surrounding body also moved.
        changes.append(
            {
                "kind": "section",
                "paragraph_id": f"<section:{index}>",
                "part": "word/document.xml",
                "section": index,
            }
        )
    for token_id, digest in candidate_snapshot["opaque_tokens"].items():
        if token_id in base_snapshot["opaque_tokens"] and base_snapshot["opaque_tokens"][token_id] != digest:
            changes.append({"kind": "opaque", "paragraph_id": f"<token:{token_id}>", "part": "document"})
    # A narrow target authorizes a change only when the paragraph it names was
    # PROVEN to be the same paragraph. Document-order pairing is deterministic
    # but it is not an identity proof: it can align leftovers without
    # establishing that the candidate paragraph is the base paragraph the
    # target names. Whole-document mode (empty target) authorizes everything,
    # so order pairing remains usable there and in the change report.
    base_ids = [str(item["id"]) for item in base_snapshot["paragraphs"]]
    proof = alignment_proof(alignment, base_ids)
    if target:
        base_items_list = base_snapshot["paragraphs"]
        candidate_items_list = candidate_snapshot["paragraphs"]
        candidate_index_by_id = {
            str(item["id"]): index for index, item in enumerate(candidate_items_list)
        }
        unproven = sorted(
            {
                str(item["paragraph_id"])
                for item in changes
                if item.get("kind") in {"semantic", "format"}
                and _target_matches(target, item, base_snapshot, candidate_snapshot)
                and (
                    (
                        item.get("state") != "added"
                        and proof.get(str(item.get("paragraph_id"))) == "order"
                    )
                    or (
                        item.get("state") == "added"
                        and not _anchored_bracket(
                            base_items_list,
                            candidate_items_list,
                            candidate_index_by_id.get(str(item.get("paragraph_id")), -1),
                            proof,
                        )
                    )
                )
            }
        )
        if unproven:
            raise ForeignEditError(
                "foreign-identity-unproven",
                "the target names a change whose paragraph identity or insertion position "
                "rests on document order alone; re-prepare with a whole-document target",
                {"paragraph_ids": unproven, "target": target},
            )
    dependencies = [item for item in changes if item.get("kind") == "dependency"]
    out_of_scope = [
        item
        for item in changes
        if not _target_matches(target, item, base_snapshot, candidate_snapshot)
    ]
    if (package["opaque_changed"] or package["inventory_changed"]) and not package["media_add"]["allowed"]:
        raise ForeignEditError(
            "foreign-opaque-or-package-changed",
            "candidate changed opaque content or package inventory",
            {
                "opaque_or_inventory": sorted(
                    set(
                        package["opaque_changed"]
                        + package["package_changed"]
                        + package["added"]
                        + package["removed"]
                    )
                ),
                "relationships_or_content_types_changed": package["inventory_changed"],
            },
        )
    unattributed_package = [
        item for item in changes if item.get("kind") == "package"
    ]
    if unattributed_package:
        raise ForeignEditError(
            "foreign-opaque-or-package-changed",
            "candidate changed a structured package part without an attribution",
            {"changes": unattributed_package},
        )
    if any(item.get("kind") == "opaque" for item in changes):
        raise ForeignEditError("foreign-opaque-changed", "candidate changed an opaque or protected region", {"changes": changes})
    if out_of_scope:
        raise ForeignEditError("foreign-out-of-scope", "candidate changed content outside target", {"changes": out_of_scope, "target": target})
    decision = "consent" if dependencies else "auto"
    return {
        "decision": decision,
        "target": target,
        "changes": changes,
        "dependencies": dependencies,
        "out_of_scope": [],
        "normalization": package["normalization"],
        "package": {
            "added": package["added"],
            "removed": package["removed"],
            "changed": package["changed"],
            "normalization": package["normalization"],
            "package_changed": package["package_changed"],
            "style_definition_changes": package["style_definition_changes"],
            "media_add": package["media_add"],
        },
        "alignment": {
            "methods": alignment["methods"],
            "unmatched_base": [base_snapshot["paragraphs"][index]["id"] for index in alignment["unmatched_base"]],
            "unmatched_candidate": [candidate_snapshot["paragraphs"][index]["id"] for index in alignment["unmatched_candidate"]],
        },
        "analysis_digest": semantic_sha256({"changes": changes, "normalization": package["normalization"], "alignment": alignment["methods"]}),
    }


def consent_token(candidate_id: str, receipt_digest: str, analysis_digest: str) -> str:
    payload = {"candidate_id": candidate_id, "receipt": receipt_digest, "analysis": analysis_digest}
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii")


def verify_consent_token(token: str, candidate_id: str, receipt_digest: str, analysis_digest: str) -> bool:
    try:
        payload = json.loads(base64.urlsafe_b64decode(str(token).encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return payload == {"candidate_id": candidate_id, "receipt": receipt_digest, "analysis": analysis_digest}


# ---------------------------------------------------------------------------
# External provenance (docx2typed-external-provenance-1)
# ---------------------------------------------------------------------------
# The engine deliberately runs no external tool, so it CANNOT observe which
# tool edited a candidate. A tool identity is therefore always
# caller-DECLARED and is never "proven"; `engine-observed` stays reserved for
# a future in-engine runner and is refused until one exists. Provenance is
# evidence about the surroundings of an edit, never an input to admission:
# no gate, decision, or consent path may read it.

PROVENANCE_SCHEMA = "docx2typed-external-provenance-1"
PROVENANCE_SOURCES = ("declared", "engine-observed", "manual")

#: A declared record may carry only these caller-supplied fields. Everything
#: else on the record is computed by the engine, so a caller cannot assert a
#: digest that the engine did not observe.
PROVENANCE_DECLARED_FIELDS = ("tool", "argv", "exit_code")

_DRIVE_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"']*")
_UNC_PATH_RE = re.compile(r"\\\\[^\s\"']*")
_POSIX_PATH_RE = re.compile(r"/(?:[^/\s\"']+/)*[^/\s\"']*")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"']+")
_SECRET_RE = re.compile(
    r"(?i)\b(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|"
    r"auth|bearer|credential|sig|signature)\b\s*[=:]\s*[^\s\"']+"
)
_OPAQUE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/_\-]{32,}={0,2}\b")


def redact_argument(value: str) -> str:
    """Replace paths, URLs, and secret-bearing values with stable tokens.

    Provenance is permanent, so it may not carry the user's directory layout,
    a remote URL, or anything that looks like a credential. Redaction is
    deterministic and order-preserving: two runs of one command redact to the
    same text, which is what makes the digest comparable."""
    text = _URL_RE.sub("<url>", value)
    text = _SECRET_RE.sub("<redacted>", text)
    text = _DRIVE_PATH_RE.sub("<path>", text)
    text = _UNC_PATH_RE.sub("<path>", text)
    text = _POSIX_PATH_RE.sub("<path>", text)
    text = _OPAQUE_TOKEN_RE.sub("<redacted>", text)
    return text


def redact_argv(argv: list[str] | tuple[str, ...] | None) -> list[str]:
    return [redact_argument(str(item)) for item in (argv or [])]


def build_provenance(
    *,
    source: str,
    receipt_digest: str,
    input_candidate_sha256: str,
    output_candidate_sha256: str,
    analysis_digest: str,
    tool_name: str | None = None,
    tool_version: str | None = None,
    binary_sha256: str | None = None,
    argv: list[str] | tuple[str, ...] | None = None,
    exit_code: int | None = None,
) -> dict[str, Any]:
    """One provenance record whose binding digests the ENGINE computed.

    The caller declares tool identity at most; the four binding digests are
    passed in by the engine, so a declared record can never attest a digest
    the engine did not itself observe."""
    if source not in PROVENANCE_SOURCES:
        raise ForeignEditError("foreign-provenance-invalid", f"unknown provenance source: {source!r}")
    if source == "engine-observed":
        raise ForeignEditError(
            "foreign-provenance-invalid",
            "engine-observed is reserved for an in-engine runner; this engine runs no external tool, "
            "so a tool identity is caller-declared",
        )
    record: dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA,
        "source": source,
        "receipt_digest": receipt_digest,
        "input_candidate_sha256": input_candidate_sha256,
        "output_candidate_sha256": output_candidate_sha256,
        "analysis_digest": analysis_digest,
    }
    if tool_name:
        tool: dict[str, Any] = {"name": str(tool_name)}
        if tool_version:
            tool["version"] = str(tool_version)
        if binary_sha256:
            if not re.fullmatch(r"[0-9a-f]{64}", str(binary_sha256)):
                raise ForeignEditError("foreign-provenance-invalid", "binary_sha256 is not a SHA-256")
            tool["binary_sha256"] = str(binary_sha256)
        record["tool"] = tool
    redacted = redact_argv(argv)
    if redacted:
        record["argv_redacted"] = redacted
        record["argv_sha256"] = semantic_sha256(redacted)
    if exit_code is not None:
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ForeignEditError("foreign-provenance-invalid", "exit_code must be an integer")
        record["exit_code"] = exit_code
    return record


def validate_provenance(data: Any) -> dict[str, Any]:
    """Structural validation only: provenance never gates admission, so this
    rejects corruption and secret leakage, not disagreement."""
    if not isinstance(data, dict) or data.get("schema") != PROVENANCE_SCHEMA:
        raise ForeignEditError("foreign-provenance-invalid", "unsupported provenance record")
    required = ("source", "receipt_digest", "input_candidate_sha256", "output_candidate_sha256", "analysis_digest")
    missing = [name for name in required if not data.get(name)]
    if missing:
        raise ForeignEditError("foreign-provenance-invalid", f"provenance is missing: {', '.join(missing)}")
    if data["source"] not in PROVENANCE_SOURCES:
        raise ForeignEditError("foreign-provenance-invalid", f"unknown provenance source: {data['source']!r}")
    if data["source"] == "engine-observed":
        raise ForeignEditError(
            "foreign-provenance-invalid",
            "engine-observed cannot be recorded: this engine runs no external tool",
        )
    tool = data.get("tool")
    if tool is not None:
        if not isinstance(tool, dict) or not tool.get("name"):
            raise ForeignEditError("foreign-provenance-invalid", "tool needs a name")
        if tool.get("binary_sha256") and not re.fullmatch(r"[0-9a-f]{64}", str(tool["binary_sha256"])):
            raise ForeignEditError("foreign-provenance-invalid", "tool.binary_sha256 is not a SHA-256")
    argv = data.get("argv_redacted")
    if argv is not None:
        if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
            raise ForeignEditError("foreign-provenance-invalid", "argv_redacted must be a list of strings")
        if str(data.get("argv_sha256") or "") != semantic_sha256(list(argv)):
            raise ForeignEditError("foreign-provenance-invalid", "argv_sha256 does not match argv_redacted")
        for item in argv:
            if redact_argument(item) != item:
                raise ForeignEditError(
                    "foreign-provenance-invalid",
                    "argv_redacted still carries a path, URL, or secret-shaped value",
                )
    return data


def declared_tool_provenance(
    *,
    tool_name: str,
    tool_version: str | None = None,
    binary_sha256: str | None = None,
    argv: list[str] | tuple[str, ...] | None = None,
    exit_code: int | None = None,
    receipt_digest: str,
    input_candidate_sha256: str,
    output_candidate_sha256: str,
    analysis_digest: str,
) -> dict[str, Any]:
    """The only shape a caller may supply: tool identity + redacted argv.
    The binding digests are engine-supplied positional facts."""
    return build_provenance(
        source="declared",
        tool_name=tool_name,
        tool_version=tool_version,
        binary_sha256=binary_sha256,
        argv=argv,
        exit_code=exit_code,
        receipt_digest=receipt_digest,
        input_candidate_sha256=input_candidate_sha256,
        output_candidate_sha256=output_candidate_sha256,
        analysis_digest=analysis_digest,
    )
