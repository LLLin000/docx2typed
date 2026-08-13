"""docx2typed decide — revision decisions (accept/reject) with governance.

Single decisions (``accept`` / ``reject`` / ``reinsert``) mutate the typed
AST in place and publish through the sync transactional path: typed.md,
edit.md, edit.state.json, format.json (token records), regions.md,
revisions.json/md and run evidence are regenerated atomically; any failure
restores every replaced file.

Accept-all / reject-all never touch the original workdir: they build a new
DOCX from the decided AST and re-extract it into a fresh clean-baseline
workdir (the normalization governance pattern), with a decisions.json audit
in the new workdir.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

try:
    from .edit import (
        EVIDENCE_FILE,
        STATE_FILE,
        _build_evidence,
        _build_revision_context,
        _now,
        _publish_sync,
        _resolve_author,
        _sha256,
        _sync_format_records,
        _write_regions,
        _write_revisions,
        classify_edit_state,
        create_edit_state,
        edit_body_sha256,
        render_edit_projection,
        require_clean_edit,
    )
    from .edit_sync import SyncPlan, _revision_attrs, _revision_token_record
    from .typed_core import (
        Paragraph,
        RevisionNode,
        TypedDocument,
        TypedError,
        apply_all_decisions,
        apply_revision_decision,
        contains_opaque,
        find_revision,
        merge_adjacent_text,
        parse_typed,
        reinsert_deleted_text,
        serialize_typed,
    )
    from .typed_docx import (
        ValidationError,
        _paragraph_placements,
        _render_paragraph,
        _write_patched_docx,
        build_workdir,
        extract_workdir,
        package_guard,
        patch_document_xml,
        validate_workdir,
        verify_workdir,
    )
except ImportError:  # direct script execution has no package context.
    from edit import (
        EVIDENCE_FILE,
        STATE_FILE,
        _build_evidence,
        _build_revision_context,
        _now,
        _publish_sync,
        _resolve_author,
        _sha256,
        _sync_format_records,
        _write_regions,
        _write_revisions,
        classify_edit_state,
        create_edit_state,
        edit_body_sha256,
        render_edit_projection,
        require_clean_edit,
    )
    from edit_sync import SyncPlan, _revision_attrs, _revision_token_record
    from typed_core import (
        Paragraph,
        RevisionNode,
        TypedDocument,
        TypedError,
        apply_all_decisions,
        apply_revision_decision,
        contains_opaque,
        find_revision,
        merge_adjacent_text,
        parse_typed,
        reinsert_deleted_text,
        serialize_typed,
    )
    from typed_docx import (
        ValidationError,
        _paragraph_placements,
        _render_paragraph,
        _write_patched_docx,
        build_workdir,
        extract_workdir,
        package_guard,
        patch_document_xml,
        validate_workdir,
        verify_workdir,
    )

DECISIONS_SCHEMA = "typed-decisions-1"
REVIEW_DECISIONS_SCHEMA = "docx2typed-review-decisions-1"


def _apply_decisions_file(
    workdir: Path, file: Path, *, include_results: bool = False
) -> dict[str, Any]:
    """Apply a review-console decisions export (accept/reject) batch.

    Each accepted or rejected entry publishes through the same transactional
    single-decision path; ``defer`` entries and entries without an action are
    skipped, and every failure is reported without rolling back the entries
    that already published.
    """
    payload = json.loads(file.read_text(encoding="utf-8"))
    if payload.get("schema") != REVIEW_DECISIONS_SCHEMA:
        raise ValidationError(
            f"unsupported decisions schema: {payload.get('schema')!r} "
            f"(expected {REVIEW_DECISIONS_SCHEMA!r})"
        )
    applied = 0
    skipped = 0
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    for entry in payload.get("decisions") or []:
        revision_key = entry.get("revision_key")
        decision_action = entry.get("decision")
        if not revision_key or decision_action not in ("accept", "reject"):
            skipped += 1
            results.append(
                {
                    "revision_key": str(revision_key or ""),
                    "decision": str(decision_action or ""),
                    "status": "not_attempted",
                }
            )
            continue
        try:
            _decide_single(
                workdir,
                revision_key,
                action=decision_action,
                expected_fingerprint=_parse_revision_key(revision_key)[3],
            )
            applied += 1
            results.append(
                {
                    "revision_key": revision_key,
                    "decision": decision_action,
                    "status": "published",
                }
            )
        except ValidationError as exc:
            errors.append(f"{revision_key}: {exc}")
            results.append(
                {
                    "revision_key": revision_key,
                    "decision": decision_action,
                    "status": "failed",
                    "error": str(exc),
                }
            )
    report = {"applied": applied, "skipped": skipped, "errors": errors}
    if include_results:
        report["results"] = results
    return report


def _parse_revision_key(revision_key: str) -> tuple[str, str, str, str]:
    parts = revision_key.split("|")
    if len(parts) != 4 or not parts[0] or not parts[1] or not parts[2] or not parts[3]:
        raise ValidationError(f"malformed revision key: {revision_key}")
    return parts[0], parts[1], parts[2], parts[3]


def _find_paragraph_with_revision(typed: TypedDocument, w_id: str) -> Paragraph | None:
    for paragraph in typed.paragraphs:
        if find_revision(paragraph, w_id) is not None:
            return paragraph
    return None


def _contains_comment_anchor(nodes: list[Any], comment_id: str) -> bool:
    """Whether a node subtree carries an anchor or reference for the comment."""
    from .typed_core import AnchorNode, InlineNode, RangeNode, RevisionNode

    for node in nodes:
        if isinstance(node, AnchorNode) and node.attrs.get("w:id") == comment_id:
            return True
        if (
            isinstance(node, InlineNode)
            and node.kind == "commentReference"
            and node.attrs.get("w:id") == comment_id
        ):
            return True
        if isinstance(node, (RangeNode, RevisionNode)) and _contains_comment_anchor(list(node.children), comment_id):
            return True
    return False


def _strip_comment_anchors(nodes: list[Any], comment_id: str) -> list[Any]:
    """Remove comment anchors and references for one comment id."""
    from .typed_core import AnchorNode, InlineNode, RangeNode, RevisionNode

    kept: list[Any] = []
    for node in nodes:
        if isinstance(node, AnchorNode) and node.attrs.get("w:id") == comment_id:
            continue
        if (
            isinstance(node, InlineNode)
            and node.kind == "commentReference"
            and node.attrs.get("w:id") == comment_id
        ):
            continue
        if isinstance(node, (RangeNode, RevisionNode)):
            node.children = _strip_comment_anchors(list(node.children), comment_id)
        kept.append(node)
    return kept


def _delete_comment(workdir: Path, comment_id: str) -> dict[str, Any]:
    """Delete one Word comment (entry + anchors + references) and publish."""
    require_clean_edit(workdir)
    typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
    format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
    removed_ids = [
        record["id"]
        for record in format_data.get("paragraphs", [])
        if record.get("part_key") == "comments" and record.get("part_entry_id") == comment_id
    ]
    if not removed_ids:
        raise ValidationError(f"comment-not-found: {comment_id}")
    typed.paragraphs = [p for p in typed.paragraphs if p.paragraph_id not in removed_ids]
    typed.deletions.extend(removed_ids)
    from .typed_core import merge_adjacent_text

    anchored_paragraphs: list[str] = []
    for paragraph in typed.paragraphs:
        if _contains_comment_anchor(paragraph.nodes, comment_id):
            anchored_paragraphs.append(paragraph.paragraph_id)
        paragraph.nodes = merge_adjacent_text(
            _strip_comment_anchors(list(paragraph.nodes), comment_id)
        )
    started_at = _now()
    diagnostics: list[str] = []
    try:
        typed_text = serialize_typed(typed)
        typed_hash = _sha256(typed_text.encode("utf-8"))
        projection_text = render_edit_projection(typed, base_typed_sha256=typed_hash)
        body_hash = edit_body_sha256(projection_text)
        new_state = create_edit_state(typed_hash, body_hash)
        from .edit_sync import SyncPlan

        plan = SyncPlan(TypedDocument(dict(typed.meta)))
        plan.document.paragraphs = typed.paragraphs
        plan.changed_ids = anchored_paragraphs
        plan.deletions = list(typed.deletions)
        format_text = _sync_format_records(workdir, format_data, plan)
        decision = {
            "action": "comment-delete",
            "comment_id": comment_id,
            "removed_paragraphs": removed_ids,
        }
        evidence = _build_evidence(
            command="docx2typed decide",
            status="ok",
            started_at=started_at,
            state_before="clean",
            typed_before=classify_edit_state(workdir)["typed_sha256"],
            typed_after=typed_hash,
            base_projection=classify_edit_state(workdir)["base_projection_sha256"],
            projection_before=classify_edit_state(workdir)["edit_body_sha256"],
            projection_after=body_hash,
            discarded=None,
            diagnostics=None,
            decisions=[decision],
        )
        _publish_sync(workdir, typed_text, projection_text, new_state, format_text, evidence)
        _write_regions(workdir, typed)
        _write_revisions(workdir, typed)
        return decision
    except ValidationError as exc:
        diagnostics.append(str(exc))
        failure = _build_evidence(
            command="docx2typed decide",
            status="error",
            started_at=started_at,
            state_before="clean",
            typed_before=classify_edit_state(workdir)["typed_sha256"],
            typed_after=classify_edit_state(workdir)["typed_sha256"],
            base_projection=classify_edit_state(workdir)["base_projection_sha256"],
            projection_before=classify_edit_state(workdir)["edit_body_sha256"],
            projection_after=classify_edit_state(workdir)["edit_body_sha256"],
            discarded=None,
            diagnostics=diagnostics,
        )
        (workdir / EVIDENCE_FILE).write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        raise


def _apply_table_op(
    workdir: Path,
    table_ref: str,
    operation: str,
    args: list[int],
    output: Path,
    new_workdir: Path,
    *,
    discard_content: bool = False,
) -> Path:
    """Apply a table structure operation and re-extract a new baseline."""
    from .typed_docx import apply_table_operation

    if not (table_ref.startswith("T") and table_ref[1:].isdigit()):
        raise ValidationError(f"invalid table reference: {table_ref}")
    table_index = int(table_ref[1:])
    require_clean_edit(workdir)
    validated = validate_workdir(workdir)
    with zipfile.ZipFile(validated.template_path) as archive:
        document_xml = archive.read("word/document.xml")
    patched = apply_table_operation(document_xml, table_index, operation, *args, discard_content=discard_content)
    output_path = Path(output).resolve()
    new_path = Path(new_workdir).resolve()
    if output_path.exists():
        raise ValidationError(f"output already exists: {output_path}")
    if new_path.exists():
        raise ValidationError(f"workdir already exists: {new_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    temp_workdir: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".typed-table-", suffix=".docx", dir=output_path.parent, delete=False) as temp:
            temp_name = temp.name
        temp_path = Path(temp_name)
        from .typed_docx import _write_patched_docx, package_guard

        _write_patched_docx(validated.template_path, temp_path, patched)
        package_guard(validated.template_path, temp_path)
        temp_workdir = Path(tempfile.mkdtemp(prefix=f".{new_path.name}-", dir=new_path.parent))
        extract_workdir(temp_path, temp_workdir)
        verify_workdir(temp_workdir, temp_path)
        os.replace(temp_path, output_path)
        shutil.rmtree(temp_workdir)
        extract_workdir(output_path, new_path)
        verify_workdir(new_path, output_path)
        decision_record = {
            "schema": DECISIONS_SCHEMA,
            "action": operation,
            "table": table_ref,
            "args": args,
            "actor": os.environ.get("DOCX2TYPED_ACTOR", "cli"),
            "started_at": _now(),
            "finished_at": _now(),
            "source_workdir": str(workdir),
        }
        (new_path / "decisions.json").write_text(
            json.dumps(decision_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return new_path
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()
        if temp_workdir is not None and temp_workdir.exists():
            shutil.rmtree(temp_workdir, ignore_errors=True)


def _decide_single(
    workdir: Path,
    revision_key: str,
    *,
    action: str,
    author: str | None = None,
    text: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Apply one decision and publish through the sync transactional path."""
    require_clean_edit(workdir)
    part, kind, w_id, fingerprint = _parse_revision_key(revision_key)
    if expected_fingerprint is not None and expected_fingerprint != fingerprint:
        raise ValidationError(
            f"revision-fingerprint-mismatch: key says {fingerprint}, confirmation says {expected_fingerprint}"
        )
    if part != "word/document.xml":
        raise ValidationError(
            f"revision-outside-editable-surface: {part} revisions can only be viewed"
        )
    typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
    paragraph = _find_paragraph_with_revision(typed, w_id)
    if paragraph is None:
        raise ValidationError(f"revision-not-found: {revision_key}")
    if contains_opaque(paragraph.nodes):
        raise ValidationError(
            f"revision-outside-editable-surface: paragraph {paragraph.paragraph_id} contains "
            "unsupported structure; its revisions can only be viewed"
        )
    format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
    session_author, author_source = _resolve_author(author)
    ctx = _build_revision_context(
        typed, format_data, workdir, mode="track",
        author=session_author, author_source=author_source,
    )
    started_at = _now()
    diagnostics: list[str] = []
    plan = SyncPlan(TypedDocument(dict(typed.meta)))
    try:
        if action == "reinsert":
            node = reinsert_deleted_text(
                paragraph,
                w_id=w_id,
                fingerprint=fingerprint,
                token_id=ctx["next_token_id"](),
                attrs=_revision_attrs(
                    "insert", ctx["next_w_id"](), session_author, ctx["date"],
                    ctx.get("date_utc", False),
                ),
                text=text,
            )
            plan.new_tokens[node.token_id] = _revision_token_record("insert", node.attrs)
            decision = {
                "w_id": w_id,
                "kind": kind,
                "action": "reinsert",
                "fingerprint": fingerprint,
                "paragraph_id": paragraph.paragraph_id,
                "operation": "new-insert-after-deletion",
                "new_w_id": node.attrs["w:id"],
            }
        else:
            decision = apply_revision_decision(
                paragraph, w_id=w_id, kind=kind, fingerprint=fingerprint, action=action
            )
        paragraph.nodes = merge_adjacent_text(paragraph.nodes)
        typed_text = serialize_typed(typed)
        typed_hash = _sha256(typed_text.encode("utf-8"))
        projection_text = render_edit_projection(typed, base_typed_sha256=typed_hash)
        body_hash = edit_body_sha256(projection_text)
        new_state = create_edit_state(typed_hash, body_hash)
        plan.document.paragraphs = typed.paragraphs
        plan.changed_ids = [paragraph.paragraph_id]
        format_text = _sync_format_records(workdir, format_data, plan)
        evidence = _build_evidence(
            command="docx2typed decide",
            status="ok",
            started_at=started_at,
            state_before="clean",
            typed_before=classify_edit_state(workdir)["typed_sha256"],
            typed_after=typed_hash,
            base_projection=classify_edit_state(workdir)["base_projection_sha256"],
            projection_before=classify_edit_state(workdir)["edit_body_sha256"],
            projection_after=body_hash,
            discarded=None,
            diagnostics=None,
            changed_ids=[paragraph.paragraph_id],
            decisions=[decision],
            author=session_author,
            author_source=author_source,
        )
        _publish_sync(workdir, typed_text, projection_text, new_state, format_text, evidence)
        _write_regions(workdir, typed)
        _write_revisions(workdir, typed)
        return decision
    except ValidationError as exc:
        diagnostics.append(str(exc))
        failure = _build_evidence(
            command="docx2typed decide",
            status="error",
            started_at=started_at,
            state_before="clean",
            typed_before=classify_edit_state(workdir)["typed_sha256"],
            typed_after=classify_edit_state(workdir)["typed_sha256"],
            base_projection=classify_edit_state(workdir)["base_projection_sha256"],
            projection_before=classify_edit_state(workdir)["edit_body_sha256"],
            projection_after=classify_edit_state(workdir)["edit_body_sha256"],
            discarded=None,
            diagnostics=diagnostics,
        )
        (workdir / EVIDENCE_FILE).write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        raise


def _decide_all(
    workdir: Path,
    action: str,
    output: Path,
    new_workdir: Path,
) -> Path:
    """Accept/reject every tracked revision in the document via byte-level
    settlement, then re-extract a clean-baseline project.

    The settlement operates on the raw XML of document.xml and every editable
    part (headers, footers, footnotes, endnotes): accept unwraps insertions
    and removes deletions; reject does the inverse with w:delText -> w:t.
    Unsupported interior bytes (fields, math, drawings, content controls)
    are copied verbatim — only revision wrapper bytes change. The original
    workdir is never mutated; the new DOCX and workdir must not already
    exist.
    """
    from .typed_docx import PART_KEYS_PATTERN, settle_xml_revisions

    require_clean_edit(workdir)
    validated = validate_workdir(workdir)
    with zipfile.ZipFile(validated.template_path) as archive:
        part_xmls = {
            match.group(1): archive.read(name)
            for name in archive.namelist()
            if (match := PART_KEYS_PATTERN.match(name))
        }
        document_xml = archive.read("word/document.xml")
    from .typed_docx import (
        _COMMENT_PARTS,
        clear_comments_from_document,
        empty_comments_part,
        settle_xml_revisions,
    )

    settled_document = settle_xml_revisions(document_xml, action)
    settled_document = clear_comments_from_document(settled_document)
    settled_parts = {
        part_key: settle_xml_revisions(part_xmls[part_key], action)
        for part_key in part_xmls
    }
    with zipfile.ZipFile(validated.template_path) as archive:
        comment_parts = {
            name: archive.read(name)
            for name in _COMMENT_PARTS
            if name in {info.filename for info in archive.infolist()}
        }
    for name, part_xml in comment_parts.items():
        settled_parts[name] = empty_comments_part(part_xml)
    output_path = Path(output).resolve()
    new_path = Path(new_workdir).resolve()
    if output_path.exists():
        raise ValidationError(f"decided output already exists: {output_path}")
    if new_path.exists():
        raise ValidationError(f"decided workdir already exists: {new_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    temp_workdir: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".typed-decide-", suffix=".docx", dir=output_path.parent, delete=False) as temp:
            temp_name = temp.name
        temp_path = Path(temp_name)
        from .typed_docx import _write_patched_docx

        _write_patched_docx(validated.template_path, temp_path, settled_document, settled_parts)
        package_guard(validated.template_path, temp_path, editable_parts=set(settled_parts))
        temp_workdir = Path(tempfile.mkdtemp(prefix=f".{new_path.name}-", dir=new_path.parent))
        extract_workdir(temp_path, temp_workdir)
        verify_workdir(temp_workdir, temp_path)
        os.replace(temp_path, output_path)
        shutil.rmtree(temp_workdir)
        extract_workdir(output_path, new_path)
        verify_workdir(new_path, output_path)
        from .typed_docx import scan_package_revisions

        settled_before = len(scan_package_revisions(validated.template_path))
        decision_record = {
            "schema": DECISIONS_SCHEMA,
            "action": action,
            "actor": os.environ.get("DOCX2TYPED_ACTOR", "cli"),
            "started_at": _now(),
            "finished_at": _now(),
            "source_workdir": str(workdir),
            "source_typed_sha256": validated.format_data.get("document_xml_sha256", ""),
            "revision_count": settled_before,
            "method": "byte-level-settlement",
            "decisions": [],
        }
        (new_path / "decisions.json").write_text(
            json.dumps(decision_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return new_path
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()
        if temp_workdir is not None and temp_workdir.exists():
            shutil.rmtree(temp_workdir, ignore_errors=True)


def decide(argv: list[str] | None = None) -> int:
    """docx2typed decide — accept/reject tracked revisions with governance."""
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="docx2typed decide",
        description=(
            "Single decisions mutate the typed AST and publish transactionally "
            "(accept/reject/reinsert). --all decisions build a new DOCX and "
            "re-extract a fresh clean-baseline workdir; the original workdir is "
            "never mutated."
        ),
    )
    parser.add_argument(
        "action",
        choices=(
            "accept", "reject", "reinsert", "accept-all", "reject-all", "comment-delete", "apply",
            "table-insert-row", "table-delete-row", "table-insert-col", "table-delete-col",
            "table-merge-cells", "table-split-cells",
        ),
    )
    parser.add_argument("revision_key", nargs="?", help="part|kind|w:id|fingerprint from revisions.json")
    parser.add_argument("--workdir", required=True, help="typed workdir")
    parser.add_argument("--file", help="review-decisions.json export to apply (action: apply)")
    parser.add_argument("--fingerprint", help="expected revision text fingerprint (defensive check)")
    parser.add_argument("--author", default=None, help="session revision author for reinsert")
    parser.add_argument("--text", default=None, help="reinsert text (default: original deleted text)")
    parser.add_argument("--output", help="decided DOCX output (accept-all/reject-all/table ops)")
    parser.add_argument("--workdir-out", help="new clean-baseline workdir (accept-all/reject-all/table ops)")
    parser.add_argument("--args", default="", help="space-separated numeric args for table ops (e.g. --args '1 0 2')")
    parser.add_argument("--discard-content", action="store_true", help="merge-cells: allow dropping the spanned cells' text (fail-closed by default)")
    args = parser.parse_args(argv)
    try:
        workdir = Path(args.workdir).resolve()
        if args.action.startswith("table-"):
            if not args.revision_key or not args.output or not args.workdir_out:
                parser.error("table ops need table ref (revision_key), --output and --workdir-out")
            numbers = [int(part) for part in args.args.split() if part.strip().isdigit()]
            new_workdir = _apply_table_op(
                workdir, args.revision_key, args.action[len("table-"):], numbers,
                Path(args.output), Path(args.workdir_out),
                discard_content=args.discard_content,
            )
            print(f"table op applied: {new_workdir}")
            return 0
        if args.action == "comment-delete":
            if not args.revision_key:
                parser.error("comment id is required for comment-delete")
            decision = _delete_comment(workdir, args.revision_key)
            print(f"deleted comment: {decision['comment_id']} ({len(decision['removed_paragraphs'])} entry paragraph(s))")
            return 0
        if args.action == "apply":
            if not args.file:
                parser.error("--file is required for apply (review-decisions.json)")
            report = _apply_decisions_file(workdir, Path(args.file))
            print(f"applied {report['applied']} decision(s), skipped {report['skipped']} (defer/comment), errors {len(report['errors'])}")
            for error in report["errors"]:
                print(f"  ERROR {error}")
            return 1 if report["errors"] else 0
        if args.action in ("accept", "reject", "reinsert"):
            if not args.revision_key:
                parser.error("revision_key is required for accept/reject/reinsert")
            if not args.fingerprint:
                parser.error("--fingerprint is required for accept/reject/reinsert (defensive addressing)")
            decision = _decide_single(
                workdir,
                args.revision_key,
                action=args.action,
                author=args.author,
                text=args.text,
                expected_fingerprint=args.fingerprint,
            )
            print(
                f"decided: {args.action} w:id={decision['w_id']} in {decision['paragraph_id']} "
                f"(operation: {decision['operation']})"
            )
            return 0
        if args.action in ("accept-all", "reject-all"):
            if not args.output or not args.workdir_out:
                parser.error("--output and --workdir-out are required for accept-all/reject-all")
            new_workdir = _decide_all(
                workdir,
                "accept" if args.action == "accept-all" else "reject",
                Path(args.output),
                Path(args.workdir_out),
            )
            report = json.loads((new_workdir / "decisions.json").read_text(encoding="utf-8"))
            print(f"decided-all: {new_workdir}")
            print(
                f"  settled {report['revision_count']} revisions via byte-level "
                "settlement; new baseline revisions.json is empty"
            )
            return 0
        parser.error("unknown action")
        return 1
    except (OSError, zipfile.BadZipFile, TypedError, ValidationError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(decide())
