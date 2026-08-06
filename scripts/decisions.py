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
    """Accept/reject every revision and re-extract a clean-baseline project.

    The original workdir is never mutated; the new DOCX and workdir must not
    already exist.
    """
    require_clean_edit(workdir)
    validated = validate_workdir(workdir)
    transformed, changes = apply_all_decisions(validated.live_paragraphs, action)
    original_by_id = {paragraph.paragraph_id: paragraph for paragraph in validated.live_paragraphs}
    replacements: list[bytes] = []
    for paragraph in transformed:
        if not paragraph.inherit:
            baseline = validated.baseline_by_id[paragraph.paragraph_id]
            original = original_by_id[paragraph.paragraph_id]
            # Unsupported-structure paragraphs replay untouched (their marks
            # and revisions stay out of the decided surface); editable
            # paragraphs that carried marks must re-render to drop them.
            if paragraph.nodes == original.nodes and (
                original.mark_revision is None or contains_opaque(paragraph.nodes)
            ):
                replacements.append(baseline.raw_xml.encode("utf-8"))
            else:
                replacements.append(
                    _render_paragraph(paragraph, baseline, validated.styles, validated.format_data.get("tokens", {}))
                )
        else:
            replacements.append(
                _render_paragraph(paragraph, validated.baseline_by_id[paragraph.inherit], validated.styles, validated.format_data.get("tokens", {}))
            )
    slots, insert_before = _paragraph_placements(transformed, len(validated.template_slices.paragraphs))
    patched_xml = patch_document_xml(
        validated.template_xml,
        validated.template_slices,
        replacements,
        slots,
        insert_before,
    )
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
        _write_patched_docx(validated.template_path, temp_path, patched_xml)
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
            "action": action,
            "actor": os.environ.get("DOCX2TYPED_ACTOR", "cli"),
            "started_at": _now(),
            "finished_at": _now(),
            "source_workdir": str(workdir),
            "source_typed_sha256": validated.format_data.get("document_xml_sha256", ""),
            "revision_count": len(changes),
            "decisions": changes,
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
    parser.add_argument("action", choices=("accept", "reject", "reinsert", "accept-all", "reject-all"))
    parser.add_argument("revision_key", nargs="?", help="part|kind|w:id|fingerprint from revisions.json")
    parser.add_argument("--workdir", required=True, help="typed workdir")
    parser.add_argument("--fingerprint", help="expected revision text fingerprint (defensive check)")
    parser.add_argument("--author", default=None, help="session revision author for reinsert")
    parser.add_argument("--text", default=None, help="reinsert text (default: original deleted text)")
    parser.add_argument("--output", help="decided DOCX output (accept-all/reject-all)")
    parser.add_argument("--workdir-out", help="new clean-baseline workdir (accept-all/reject-all)")
    args = parser.parse_args(argv)
    try:
        workdir = Path(args.workdir).resolve()
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
                f"  settled {report['revision_count']} editable-surface revisions; "
                "paragraphs with unsupported structure replay untouched "
                "(their revisions stay in the new baseline — see revisions.json editable=false)"
            )
            return 0
        parser.error("unknown action")
        return 1
    except (OSError, zipfile.BadZipFile, TypedError, ValidationError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(decide())
