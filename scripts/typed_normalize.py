"""Explicit, auditable Unicode vertical normalization."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Iterable

try:
    from .typed_core import (
        NS_W,
        Paragraph,
        RangeNode,
        Style,
        StyleRegistry,
        TextNode,
        TypedError,
        etree_xml,
        local_name,
        visible_text,
        w,
    )
    from .typed_docx import (
        ValidationError,
        _paragraph_placements,
        _render_paragraph,
        _write_patched_docx,
        package_guard,
        patch_document_xml,
        validate_workdir,
        verify_workdir,
    )
    from .audit_contract import (
        AUDIT_SCHEMA,
        POLICY_SCHEMA,
        create_audit,
        create_scan_artifact,
        create_snapshot,
        require_approved,
        validate_scan_artifact,
    )
except ImportError:
    from typed_core import (
        NS_W,
        Paragraph,
        RangeNode,
        Style,
        StyleRegistry,
        TextNode,
        TypedError,
        etree_xml,
        local_name,
        visible_text,
        w,
    )
    from typed_docx import (
        ValidationError,
        _paragraph_placements,
        _render_paragraph,
        _write_patched_docx,
        package_guard,
        patch_document_xml,
        validate_workdir,
        verify_workdir,
    )
    from audit_contract import (
        AUDIT_SCHEMA,
        POLICY_SCHEMA,
        create_audit,
        create_scan_artifact,
        create_snapshot,
        require_approved,
        validate_scan_artifact,
    )
CATALOG_PATH = Path(__file__).with_name("unicode_vertical_catalog.json")


def _catalog_payload(data: dict[str, Any]) -> bytes:
    base = {key: value for key, value in data.items() if key != "catalog_hash"}
    return json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_catalog(path: str | Path = CATALOG_PATH) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read pinned Unicode catalog: {exc}") from exc
    if data.get("schema") != "unicode-vertical-catalog-1" or not data.get("unicode_version"):
        raise ValidationError("incompatible Unicode vertical catalog")
    expected = hashlib.sha256(_catalog_payload(data)).hexdigest()
    if data.get("catalog_hash") != expected:
        raise ValidationError("Unicode vertical catalog hash mismatch")
    entries = data.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise ValidationError("Unicode vertical catalog is empty")
    for codepoint, entry in entries.items():
        if not {"source", "target", "vertical", "class", "reversible"}.issubset(entry):
            raise ValidationError(f"incomplete catalog entry: {codepoint}")
        if entry["class"] not in {"approved", "ambiguous", "manual", "unsupported"}:
            raise ValidationError(f"invalid catalog class: {codepoint}")
    return data


def _walk_text(nodes: Iterable[Any], prefix: tuple[int, ...] = ()) -> Iterable[tuple[TextNode, tuple[int, ...]]]:
    """Yield (text node, child-index path from paragraph root)."""
    for index, node in enumerate(nodes):
        path = prefix + (index,)
        if isinstance(node, TextNode):
            yield node, path
        elif isinstance(node, RangeNode):
            yield from _walk_text(node.children, path)


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - 12):min(len(text), end + 12)]


def find_candidates(
    document: Any,
    catalog: dict[str, Any] | None = None,
    styles: StyleRegistry | None = None,
) -> list[dict[str, Any]]:
    catalog = catalog or load_catalog()
    entries = catalog["entries"]
    candidates: list[dict[str, Any]] = []
    candidate_count = 0
    for paragraph in document.paragraphs:
        occurrence = 0
        paragraph_text = visible_text(paragraph.nodes)
        offset = 0
        for text_node, path in _walk_text(paragraph.nodes):
            style = styles.styles.get(text_node.style_id) if styles else None
            for char_index, char in enumerate(text_node.text):
                codepoint = f"U+{ord(char):04X}"
                entry = entries.get(codepoint)
                if entry:
                    occurrence += 1
                    candidate_count += 1
                    occurrence_id = f"{paragraph.paragraph_id}-V{occurrence:04d}"
                    node_path = paragraph.paragraph_id + "/" + "/".join(f"n{index}" for index in path) + f"/c{char_index}"
                    candidates.append(
                        {
                            "candidate_id": f"C{candidate_count:05d}",
                            "occurrence_id": occurrence_id,
                            "paragraph_id": paragraph.paragraph_id,
                            "node_path": node_path,
                            "visible_offset": offset,
                            "codepoint": codepoint,
                            "source": char,
                            "name": entry.get("name", ""),
                            "category": entry["class"],
                            "classification": entry["class"],
                            "vertical": entry["vertical"],
                            "proposed_target": entry["target"],
                            "reversible": entry["reversible"],
                            "style_id": text_node.style_id,
                            "word_style_label": style.label if style else "",
                            "word_style_features": dict(style.features) if style else {},
                            "word_vert_align": style.features.get("vertAlign") if style else None,
                            "context": _context(paragraph_text, offset, offset + 1),
                        }
                    )
                offset += 1
    return candidates


def _compose_style(registry: StyleRegistry, style_id: str, vertical: str) -> str:
    style = registry.require(style_id)
    root = ET.fromstring(style.rpr)
    existing_vertical = None
    for child in root:
        name = local_name(child.tag)
        if name == "vertAlign":
            existing_vertical = child.attrib.get(w("val"), child.attrib.get("val"))
        elif name == "position":
            raise ValidationError(f"style {style_id} has conflicting position formatting")
    if existing_vertical and existing_vertical != vertical:
        raise ValidationError(f"style {style_id} has conflicting vertAlign")
    if existing_vertical == vertical:
        return style_id
    root.append(ET.Element(w("vertAlign"), {w("val"): vertical}))
    return registry.ensure(etree_xml(root), label=f"{style.label}, vertAlign={vertical}")


def _transform_nodes(nodes: list[Any], decisions: dict[str, dict[str, Any]], catalog: dict[str, Any], registry: StyleRegistry, paragraph_id: str, counter: list[int], changes: dict[str, dict[str, Any]] | None = None) -> list[Any]:
    entries = catalog["entries"]
    result: list[Any] = []
    for node in nodes:
        if isinstance(node, RangeNode):
            node_copy = copy.deepcopy(node)
            node_copy.children = _transform_nodes(node.children, decisions, catalog, registry, paragraph_id, counter, changes)
            result.append(node_copy)
            continue
        if not isinstance(node, TextNode):
            result.append(copy.deepcopy(node))
            continue
        chunks: list[TextNode] = []
        current_style = node.style_id
        current_text: list[str] = []
        for char in node.text:
            codepoint = f"U+{ord(char):04X}"
            entry = entries.get(codepoint)
            if entry:
                counter[0] += 1
                occurrence_id = f"{paragraph_id}-V{counter[0]:04d}"
            else:
                occurrence_id = ""
            decision = decisions.get(occurrence_id, {}).get("decision", "preserve") if occurrence_id else "preserve"
            replacement = char
            style_id = current_style
            if entry and decision == "convert":
                if entry["class"] != "approved" or not entry["target"]:
                    raise ValidationError(f"catalog entry cannot be converted: {codepoint}")
                replacement = entry["target"]
                style_id = _compose_style(registry, current_style, entry["vertical"])
                if changes is not None:
                    changes[occurrence_id] = {"new_style_id": style_id}
            if style_id != current_style or (current_text and replacement != char and len(replacement) != 1):
                if current_text:
                    chunks.append(TextNode(current_style, "".join(current_text)))
                    current_text = []
                current_style = style_id
            current_text.append(replacement)
        if current_text:
            chunks.append(TextNode(current_style, "".join(current_text)))
        result.extend(chunks)
    return result


def _policy_decisions(policy: dict[str, Any], candidates: list[dict[str, Any]], catalog: dict[str, Any], template_sha256: str) -> dict[str, dict[str, Any]]:
    if policy.get("schema") != "vertical-normalization-policy-1":
        raise ValidationError("incompatible normalization policy")
    if policy.get("catalog_hash") != catalog["catalog_hash"]:
        raise ValidationError("normalization policy uses a different catalog")
    if policy.get("template_sha256") != template_sha256:
        raise ValidationError("normalization policy uses a different workdir template")
    raw_decisions = policy.get("decisions", {})
    if not isinstance(raw_decisions, dict):
        raise ValidationError("normalization decisions must be an object")
    result: dict[str, dict[str, Any]] = {}
    profile = policy.get("profile", "selective")
    candidate_map = {candidate["occurrence_id"]: candidate for candidate in candidates}
    for occurrence_id, candidate in candidate_map.items():
        decision = raw_decisions.get(occurrence_id)
        if decision is None and profile == "all":
            decision = "convert" if candidate["category"] == "approved" else "preserve"
        if decision not in {"convert", "preserve"}:
            raise ValidationError(f"missing normalization decision: {occurrence_id}")
        if decision == "convert" and (candidate["category"] != "approved" or not candidate["proposed_target"]):
            raise ValidationError(f"non-approved candidate cannot be converted: {occurrence_id}")
        result[occurrence_id] = {"decision": decision, **candidate}
    unknown = set(raw_decisions) - set(candidate_map)
    if unknown:
        raise ValidationError("normalization policy references unknown occurrences: " + ", ".join(sorted(unknown)))
    return result


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def candidate_report(workdir: str | Path) -> list[dict[str, Any]]:
    validated = validate_workdir(workdir)
    return find_candidates(validated.typed, load_catalog(), validated.styles)


def scan_workdir(
    workdir: str | Path,
    catalog: dict[str, Any] | None = None,
    project_id: str | None = None,
    model_sha256: str | None = None,
) -> dict[str, Any]:
    """Build the scan artifact for a workdir (single source of truth).

    ``audit scan`` and the policy-2 apply path both call this function so a
    scan file written by the CLI is byte-identical to the scan validated at
    apply time. Snapshot derivation: baseline = workdir template fingerprint,
    draft = SHA-256 of typed.md, catalog/model/project from the pinned catalog
    and deterministic defaults unless overridden.
    """
    validated = validate_workdir(workdir)
    catalog = catalog or load_catalog()
    snapshot = create_snapshot(
        project_id=project_id,
        baseline_sha256=validated.format_data["template_sha256"],
        draft_snapshot_sha256=hashlib.sha256((Path(workdir) / "typed.md").read_bytes()).hexdigest(),
        model_sha256=model_sha256,
        catalog_sha256=catalog["catalog_hash"],
    )
    candidates = find_candidates(validated.typed, catalog, validated.styles)
    return create_scan_artifact(snapshot=snapshot, candidates=candidates)


def _contract_scan(policy: dict[str, Any], validated: Any, catalog: dict[str, Any], workdir: str | Path, scan_path: str | Path | None, project_id: str | None, model_sha256: str | None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Policy-2 apply gate: build/validate the scan and approved decisions."""
    if scan_path is not None:
        try:
            scan = json.loads(Path(scan_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read scan artifact: {exc}") from exc
        scan = validate_scan_artifact(scan)
        current = scan_workdir(workdir, catalog, project_id, model_sha256)
        snapshot_fields = (
            "project_id",
            "baseline_sha256",
            "draft_snapshot_sha256",
            "model_sha256",
            "catalog_sha256",
            "scanner_contract_version",
        )
        for field in snapshot_fields:
            if scan["snapshot"].get(field) != current["snapshot"].get(field):
                raise ValidationError(f"scan artifact is stale: {field} changed since scan")
        if scan["scan_artifact_sha256"] != current["scan_artifact_sha256"]:
            raise ValidationError("scan artifact is stale: current candidate set differs from reviewed scan")
    else:
        scan = scan_workdir(workdir, catalog, project_id, model_sha256)
    require_approved(policy, scan=scan, catalog_sha256=catalog["catalog_hash"])
    candidate_map = {candidate["occurrence_id"]: candidate for candidate in scan["candidates"]}
    decisions: dict[str, dict[str, Any]] = {}
    for occurrence_id, decision in policy["decisions"].items():
        merged = {**candidate_map[occurrence_id]}
        merged["decision"] = decision["decision"]
        merged["actor"] = decision["actor"]
        if "rationale" in decision and decision["rationale"] is not None:
            merged["rationale"] = decision["rationale"]
        decisions[occurrence_id] = merged
    return scan, decisions


def normalize_workdir(
    workdir: str | Path,
    policy_path: str | Path,
    output: str | Path,
    normalized_workdir: str | Path,
    scan_path: str | Path | None = None,
    project_id: str | None = None,
    model_sha256: str | None = None,
) -> Path:
    validated = validate_workdir(workdir)
    catalog = load_catalog()
    try:
        policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read normalization policy: {exc}") from exc
    schema = policy.get("schema")
    if schema == POLICY_SCHEMA:
        scan, decisions = _contract_scan(policy, validated, catalog, workdir, scan_path, project_id, model_sha256)
    elif schema == "vertical-normalization-policy-1":
        # ponytail: legacy policy-1 retained so the existing acceptance suite keeps its exact contract.
        candidates = find_candidates(validated.typed, catalog, validated.styles)
        decisions = _policy_decisions(policy, candidates, catalog, validated.format_data["template_sha256"])
        scan = None
    else:
        raise ValidationError("incompatible normalization policy")
    changes: dict[str, dict[str, Any]] = {}
    registry = StyleRegistry(dict(validated.styles.styles))
    transformed: list[Paragraph] = []
    for paragraph in validated.live_paragraphs:
        transformed.append(
            paragraph.__class__(
                paragraph_id=paragraph.paragraph_id,
                base_style=paragraph.base_style,
                nodes=_transform_nodes(paragraph.nodes, decisions, catalog, registry, paragraph.paragraph_id, [0], changes),
                p_open=paragraph.p_open,
                ppr=paragraph.ppr,
                raw_xml=paragraph.raw_xml,
                section_bearing=paragraph.section_bearing,
                editable=paragraph.editable,
                inherit=paragraph.inherit,
                original_index=paragraph.original_index,
            )
        )
    original_by_id = {paragraph.paragraph_id: paragraph for paragraph in validated.live_paragraphs}
    replacements: list[bytes] = []
    for paragraph in transformed:
        if not paragraph.inherit:
            baseline = validated.baseline_by_id[paragraph.paragraph_id]
            if paragraph.nodes == original_by_id[paragraph.paragraph_id].nodes:
                replacements.append(baseline.raw_xml.encode("utf-8"))
            else:
                replacements.append(_render_paragraph(paragraph, baseline, registry, validated.format_data.get("tokens", {})))
        else:
            replacements.append(_render_paragraph(paragraph, validated.baseline_by_id[paragraph.inherit], registry, validated.format_data.get("tokens", {})))
    slots, insert_before = _paragraph_placements(transformed, len(validated.template_slices.paragraphs))
    patched_xml = patch_document_xml(
        validated.template_xml,
        validated.template_slices,
        replacements,
        slots,
        insert_before,
    )
    output_path = Path(output).resolve()
    new_workdir = Path(normalized_workdir).resolve()
    if output_path.exists():
        raise ValidationError(f"normalized output already exists: {output_path}")
    if new_workdir.exists():
        raise ValidationError(f"normalized workdir already exists: {new_workdir}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    new_workdir.parent.mkdir(parents=True, exist_ok=True)
    try:
        from .typed_docx import extract_workdir
    except ImportError:
        from typed_docx import extract_workdir

    temp_name: str | None = None
    temp_workdir: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".typed-normalize-", suffix=".docx", dir=output_path.parent, delete=False) as temp:
            temp_name = temp.name
        temp_path = Path(temp_name)
        _write_patched_docx(validated.template_path, temp_path, patched_xml)
        package_guard(validated.template_path, temp_path)
        temp_workdir = Path(tempfile.mkdtemp(prefix=f".{new_workdir.name}-", dir=new_workdir.parent))
        extract_workdir(temp_path, temp_workdir)
        verify_workdir(temp_workdir, temp_path)
        if scan is None:
            audit = [
                {
                    "occurrence_id": occurrence_id,
                    "paragraph_id": decision["paragraph_id"],
                    "old_text": decision["source"],
                    "new_text": decision["proposed_target"] if decision["decision"] == "convert" else decision["source"],
                    "old_style_id": decision["style_id"],
                    "word_style_label": decision.get("word_style_label", ""),
                    "word_vert_align": decision.get("word_vert_align"),
                    "category": decision["category"],
                    "reversible": decision["reversible"],
                    "vertical": decision["vertical"],
                    "decision": decision["decision"],
                    "context": decision["context"],
                }
                for occurrence_id, decision in sorted(decisions.items())
            ]
            policy_copy = dict(policy)
            policy_copy["catalog_hash"] = catalog["catalog_hash"]
            policy_copy["catalog_version"] = catalog["unicode_version"]
            policy_copy["governance_status"] = "legacy-unaudited"
            audit_doc: dict[str, Any] = {
                "schema": "vertical-normalization-audit-1",
                "governance_status": "legacy-unaudited",
                "source_workdir": Path(workdir).name,
                "source_template_sha256": validated.format_data["template_sha256"],
                "catalog_hash": catalog["catalog_hash"],
                "catalog_version": catalog["unicode_version"],
                "decisions": audit,
            }
        else:
            audit_doc = create_audit(
                policy=policy,
                scan=scan,
                changes=changes,
                source_workdir=Path(workdir).name,
                catalog_version=catalog["unicode_version"],
            )
            policy_copy = dict(policy)
            policy_copy["catalog_version"] = catalog["unicode_version"]
        _write_json(temp_workdir / "normalization.policy.json", policy_copy)
        _write_json(temp_workdir / "normalization.audit.json", audit_doc)
        format_path = temp_workdir / "format.json"
        format_data = json.loads(format_path.read_text(encoding="utf-8"))
        format_data["source"] = output_path.name
        format_data["source_path"] = os.path.relpath(output_path, temp_workdir)
        _write_json(format_path, format_data)
        typed_path = temp_workdir / "typed.md"
        typed_source = typed_path.read_text(encoding="utf-8")
        typed_source = typed_source.replace(f' source="{temp_path.name}"', f' source="{output_path.name}"', 1)
        typed_path.write_text(typed_source, encoding="utf-8", newline="\n")
        os.replace(temp_workdir, new_workdir)
        temp_workdir = None
        os.replace(temp_path, output_path)
        temp_name = None
        verify_workdir(new_workdir, output_path)
    except Exception:
        if new_workdir.exists():
            shutil.rmtree(new_workdir, ignore_errors=True)
        if output_path.exists():
            output_path.unlink()
        raise
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()
        if temp_workdir and temp_workdir.exists():
            shutil.rmtree(temp_workdir, ignore_errors=True)
    return new_workdir


def normalize(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docx2typed normalize")
    parser.add_argument("workdir", help="typed workdir")
    parser.add_argument("--candidates", action="store_true", help="write candidate report")
    parser.add_argument("--policy", help="normalization policy JSON")
    parser.add_argument("--legacy-policy-1", action="store_true", help="explicitly allow unaudited policy-1 compatibility")
    parser.add_argument("-o", "--output", help="normalized DOCX")
    parser.add_argument("--workdir-out", help="new normalized workdir")
    args = parser.parse_args(argv)
    try:
        if args.candidates or not args.policy:
            report = candidate_report(args.workdir)
            payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                Path(args.output).write_text(payload, encoding="utf-8", newline="\n")
                print(f"candidates: {args.output}")
            else:
                print(payload, end="")
            return 0
        if not args.output or not args.workdir_out:
            raise ValidationError("--output and --workdir-out are required with --policy")
        try:
            policy_schema = json.loads(Path(args.policy).read_text(encoding="utf-8")).get("schema")
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read normalization policy: {exc}") from exc
        if policy_schema == "vertical-normalization-policy-1":
            if not args.legacy_policy_1:
                raise ValidationError("legacy policy-1 requires --legacy-policy-1; use audit scan/apply for governed normalization")
            print("WARNING: policy-1 normalization is legacy-unaudited; use audit scan/apply for governed normalization.", file=sys.stderr)
        result = normalize_workdir(args.workdir, args.policy, args.output, args.workdir_out)
        print(f"normalized-workdir: {result}")
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0
