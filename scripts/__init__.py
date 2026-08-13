"""docx2typed — typed-mode DOCX text editing with locked structure."""

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable

try:
    from .extract import extract
    from .view import view
    from .build import build
    from .verify import validate, verify
    from .typed_core import TypedError
    from .typed_docx import (
        build_workdir,
        extract_workdir,
        validate_workdir,
        verify_workdir,
    )
except ImportError:  # direct script execution has no package context.
    from extract import extract
    from view import view
    from build import build
    from verify import validate, verify
    from typed_core import TypedError
    from typed_docx import (
        build_workdir,
        extract_workdir,
        validate_workdir,
        verify_workdir,
    )

try:
    from .typed_normalize import normalize
except ImportError:
    from typed_normalize import normalize

try:
    from .audit import audit
except ImportError:
    from audit import audit

try:
    from .edit import edit, require_clean_edit
except ImportError:
    from edit import edit, require_clean_edit

try:
    from .decisions import decide
except ImportError:
    from decisions import decide

try:
    from .protocol import (
        base_evidence_payload,
        canonical_operation_input,
        derived_workdir_manifest,
        diagnostic,
        engine_descriptor,
        file_sha256,
        new_operation_id,
        operation_ledger,
        publish_run_evidence,
        result_envelope,
        run_evidence,
        semantic_sha256,
        typed_path,
    )
except ImportError:
    from protocol import (
        base_evidence_payload,
        canonical_operation_input,
        derived_workdir_manifest,
        diagnostic,
        engine_descriptor,
        file_sha256,
        new_operation_id,
        operation_ledger,
        publish_run_evidence,
        result_envelope,
        run_evidence,
        semantic_sha256,
        typed_path,
    )


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


class _InvocationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _JsonParser(argparse.ArgumentParser):
    """argparse that reports invocation errors without printing to stderr."""

    def error(self, message: str) -> None:  # noqa: D102 - argparse hook
        raise _InvocationError(message)


class _DomainFailure(Exception):
    def __init__(self, failure: dict[str, Any]) -> None:
        super().__init__(failure["message"])
        self.diagnostic = failure


def _invocation_failure(operation: str, message: str, argv: list[str]) -> int:
    _print_json(
        result_envelope(
            operation,
            "failure",
            data={"operation_id": None},
            diagnostics=[
                diagnostic(
                    "invalid-arguments",
                    message,
                    details={"expected": ["see docx2typed --help"], "actual": argv},
                )
            ],
        )
    )
    return 2


def _domain_failure(operation: str, code: str, message: str, operation_id: str | None) -> int:
    _print_json(
        result_envelope(
            operation,
            "failure",
            data={"operation_id": operation_id},
            diagnostics=[diagnostic(code, message)],
        )
    )
    return 1


def _validate_json(argv):
    if len(argv) != 1:
        return _invocation_failure(
            "validate", "validate requires exactly one workdir", argv
        )
    try:
        if not Path(argv[0]).is_dir():
            raise FileNotFoundError(f"typed workdir not found: {Path(argv[0]).resolve()}")
        checked = validate_workdir(argv[0])
        require_clean_edit(argv[0])
    except FileNotFoundError as exc:
        failure = diagnostic("workdir-not-found", str(exc))
    except PermissionError as exc:
        failure = diagnostic("workdir-unreadable", str(exc))
    except (zipfile.BadZipFile, TypedError) as exc:
        failure = diagnostic("workdir-invalid", str(exc))
    except OSError as exc:
        failure = diagnostic("workdir-unreadable", str(exc))
    else:
        _print_json(result_envelope(
            "validate",
            "success",
            data={
                "valid": True,
                "workdir": typed_path(checked.path),
                "warnings": checked.warnings,
            },
        ))
        return 0
    _print_json(result_envelope("validate", "failure", diagnostics=[failure]))
    return 1


# --------------------------------------------------------------------------
# Machine mode: one docx2typed-result-1 envelope per finite operation
# --------------------------------------------------------------------------

def _run_json_operation(
    operation: str,
    *,
    operation_id: str | None,
    canonical_args: dict[str, Any],
    anchor: Path,
    directory: bool,
    evidence_path: Path,
    run: Callable[[], tuple[str, dict[str, Any], str, dict[str, Any], list[dict[str, Any]]]],
) -> int:
    """Execute one finite operation under the Result/Evidence/Operation-ID
    contract. ``run`` returns (outcome, data, kind, payload, diagnostics) or
    raises _DomainFailure (mapped, exit 1). Success and partial outcomes
    publish run evidence; a publish failure can never report success.
    Identical retries replay the original envelope; changed canonical input
    fails ``operation-id-reused`` with no second effect."""
    op_id = operation_id if operation_id else new_operation_id()
    canonical = canonical_operation_input(operation, canonical_args)
    record = operation_ledger.lookup(op_id) or operation_ledger.lookup_persisted(
        op_id, anchor, directory=directory
    )
    if record is not None:
        if record["input_sha256"] == canonical:
            envelope = record["envelope"]
            _print_json(envelope)
            return 0 if envelope["outcome"] == "success" else 1
        envelope = result_envelope(
            operation,
            "failure",
            data={"operation_id": op_id},
            diagnostics=[
                diagnostic(
                    "operation-id-reused",
                    f"operation_id {op_id!r} was already used with different canonical input",
                )
            ],
        )
        _print_json(envelope)
        return 1
    try:
        outcome, data, kind, payload, diagnostics = run()
    except _DomainFailure as exc:
        _print_json(
            result_envelope(
                operation,
                "failure",
                data={"operation_id": op_id},
                diagnostics=[exc.diagnostic],
            )
        )
        return 1
    evidence = run_evidence(
        operation, outcome, kind=kind, operation_id=op_id, payload=payload
    )
    try:
        publish_run_evidence(evidence_path, evidence)
    except OSError as exc:
        envelope = result_envelope(
            operation,
            "failure",
            data={"operation_id": op_id},
            diagnostics=[
                diagnostic(
                    "evidence-publish-failed",
                    f"required run evidence could not be published: {exc}",
                )
            ],
        )
        operation_ledger.record(op_id, canonical, envelope, anchor, directory=directory)
        _print_json(envelope)
        return 1
    envelope = result_envelope(
        operation,
        outcome,
        data={"operation_id": op_id, **data},
        diagnostics=diagnostics,
        evidence=[evidence],
    )
    operation_ledger.record(op_id, canonical, envelope, anchor, directory=directory)
    _print_json(envelope)
    return 0 if outcome == "success" else 1


def _workdir_manifest_sha256(workdir: Path) -> str:
    return semantic_sha256(derived_workdir_manifest(workdir))


def _map_workdir_failure(operation: str, exc: Exception, operation_id: str | None) -> int:
    if isinstance(exc, FileNotFoundError):
        return _domain_failure(operation, "workdir-not-found", str(exc), operation_id)
    if isinstance(exc, (PermissionError, OSError)):
        return _domain_failure(operation, "workdir-unreadable", str(exc), operation_id)
    return _domain_failure(operation, "workdir-invalid", str(exc), operation_id)


def _extract_json(argv: list[str]) -> int:
    parser = _JsonParser(prog="docx2typed extract", add_help=False)
    parser.add_argument("input", help="source .docx")
    parser.add_argument("-o", "--outdir", default=".", help="typed workdir")
    parser.add_argument("--operation-id", default=None, help="retry identity (default: generated)")
    try:
        args = parser.parse_args(argv)
    except _InvocationError as exc:
        return _invocation_failure("extract", exc.message, argv)

    source = Path(args.input)
    if not source.is_file():
        return _domain_failure("extract", "input-not-found", f"source file not found: {source}", args.operation_id)
    source_sha256 = file_sha256(source)
    anchor = Path(args.outdir).resolve()
    canonical_args = {
        "input": str(args.input),
        "outdir": str(args.outdir),
        "source_sha256": source_sha256,
    }

    def run():
        try:
            workdir = extract_workdir(source, args.outdir)
        except (OSError, zipfile.BadZipFile, TypedError) as exc:
            code = "workdir-unreadable" if isinstance(exc, OSError) else "workdir-invalid"
            raise _DomainFailure(diagnostic(code, str(exc))) from exc
        payload = {
            **base_evidence_payload(),
            "inputs": {"source": {"sha256": source_sha256}},
            "outputs": {
                "workdir": {"manifest_sha256": _workdir_manifest_sha256(workdir)}
            },
            "checks": [{"name": "workdir-extracted", "status": "pass"}],
        }
        return "success", {"workdir": typed_path(workdir)}, "mutation", payload, []

    return _run_json_operation(
        "extract",
        operation_id=args.operation_id,
        canonical_args=canonical_args,
        anchor=anchor,
        directory=True,
        evidence_path=anchor / "run.evidence.json",
        run=run,
    )


def _edit_json(argv: list[str]) -> int:
    parser = _JsonParser(prog="docx2typed edit", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    status_parser = sub.add_parser("status", add_help=False)
    status_parser.add_argument("workdir")
    refresh_parser = sub.add_parser("refresh", add_help=False)
    refresh_parser.add_argument("workdir")
    refresh_parser.add_argument("--init", action="store_true")
    refresh_parser.add_argument("--discard", action="store_true")
    refresh_parser.add_argument("--operation-id", default=None)
    sync_parser = sub.add_parser("sync", add_help=False)
    sync_parser.add_argument("workdir")
    sync_parser.add_argument("--track", action="store_true")
    sync_parser.add_argument("--no-track", action="store_true")
    sync_parser.add_argument("--author", default=None)
    sync_parser.add_argument("--operation-id", default=None)
    try:
        args = parser.parse_args(argv)
    except _InvocationError as exc:
        return _invocation_failure("edit", exc.message, argv)

    from .edit import edit_status, refresh_edit_projection, sync_edit_projection

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        return _domain_failure("edit", "workdir-not-found", f"typed workdir not found: {workdir}", getattr(args, "operation_id", None))

    if args.command == "status":
        try:
            status = edit_status(workdir)
        except (OSError, zipfile.BadZipFile, TypedError) as exc:
            return _map_workdir_failure("edit", exc, None)
        _print_json(result_envelope("edit", "success", data=status))
        return 0

    manifest_before = _workdir_manifest_sha256(workdir)
    canonical_args = {
        "workdir": str(workdir),
        "command": args.command,
    }
    if args.command == "refresh":
        canonical_args["init"] = args.init
        canonical_args["discard"] = args.discard

        def run():
            try:
                state_path = refresh_edit_projection(workdir, init=args.init, discard=args.discard)
            except (OSError, zipfile.BadZipFile, TypedError) as exc:
                code = "workdir-unreadable" if isinstance(exc, OSError) else "workdir-invalid"
                raise _DomainFailure(diagnostic(code, str(exc))) from exc
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(workdir)}},
                "checks": [{"name": "edit-refresh", "status": "pass"}],
            }
            return "success", {"refreshed": True, "edit_state": typed_path(state_path)}, "mutation", payload, []
    else:
        canonical_args["track"] = "track" if args.track else ("no-track" if args.no_track else None)
        canonical_args["author"] = args.author
        track: bool | None = True if args.track else (False if args.no_track else None)

        def run():
            try:
                state_path, warnings, changed_ids = sync_edit_projection(
                    workdir, track=track, author=args.author
                )
            except (OSError, zipfile.BadZipFile, TypedError) as exc:
                code = "workdir-unreadable" if isinstance(exc, OSError) else "workdir-invalid"
                raise _DomainFailure(diagnostic(code, str(exc))) from exc
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(workdir)}},
                "checks": [{"name": "edit-sync", "status": "pass"}],
            }
            return (
                "success",
                {
                    "state": "clean",
                    "edit_state": typed_path(state_path),
                    "changed_paragraph_ids": changed_ids,
                    "warnings": warnings,
                },
                "mutation",
                payload,
                [],
            )

    return _run_json_operation(
        "edit",
        operation_id=args.operation_id,
        canonical_args=canonical_args,
        anchor=workdir,
        directory=True,
        evidence_path=workdir / "run.evidence.json",
        run=run,
    )


def _build_json(argv: list[str]) -> int:
    parser = _JsonParser(prog="docx2typed build", add_help=False)
    parser.add_argument("workdir", help="typed workdir")
    parser.add_argument("-o", "--output", default=None, help="output .docx")
    parser.add_argument("--operation-id", default=None)
    try:
        args = parser.parse_args(argv)
    except _InvocationError as exc:
        return _invocation_failure("build", exc.message, argv)

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        return _domain_failure("build", "workdir-not-found", f"typed workdir not found: {workdir}", args.operation_id)
    manifest_before = _workdir_manifest_sha256(workdir)
    output = (
        Path(args.output).resolve()
        if args.output
        else workdir.parent / f"{workdir.name}.docx"
    )
    canonical_args = {
        "workdir": str(workdir),
        "output": str(args.output) if args.output else None,
    }

    def run():
        try:
            built = build_workdir(workdir, args.output)
        except (OSError, zipfile.BadZipFile, TypedError) as exc:
            raise _DomainFailure(diagnostic("workdir-invalid", str(exc))) from exc
        payload = {
            **base_evidence_payload(),
            "inputs": {"workdir": {"manifest_sha256": manifest_before}},
            "outputs": {
                "docx": {"sha256": file_sha256(built), "bytes": built.stat().st_size}
            },
            "checks": [{"name": "build", "status": "pass"}],
        }
        return "success", {"output": typed_path(built)}, "build", payload, []

    return _run_json_operation(
        "build",
        operation_id=args.operation_id,
        canonical_args=canonical_args,
        anchor=output,
        directory=False,
        evidence_path=Path(str(output) + ".evidence.json"),
        run=run,
    )


def _verify_json(argv: list[str]) -> int:
    parser = _JsonParser(prog="docx2typed verify", add_help=False)
    parser.add_argument("workdir", help="typed workdir")
    parser.add_argument("output", help="built .docx")
    try:
        args = parser.parse_args(argv)
    except _InvocationError as exc:
        return _invocation_failure("verify", exc.message, argv)

    workdir = Path(args.workdir).resolve()
    output = Path(args.output).resolve()
    if not workdir.is_dir():
        return _domain_failure("verify", "workdir-not-found", f"typed workdir not found: {workdir}", None)
    if not output.is_file():
        return _domain_failure("verify", "input-not-found", f"output DOCX not found: {output}", None)
    manifest = _workdir_manifest_sha256(workdir)
    output_sha256 = file_sha256(output)
    try:
        verify_workdir(workdir, output)
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        return _map_workdir_failure("verify", exc, None)
    op_id = new_operation_id()
    payload = {
        **base_evidence_payload(),
        "inputs": {"workdir": {"manifest_sha256": manifest}},
        "outputs": {"docx": {"sha256": output_sha256}},
        "verdict": "pass",
        "checks": [{"name": "independent-verification", "status": "pass"}],
    }
    evidence = run_evidence("verify", "success", kind="verify", operation_id=op_id, payload=payload)
    evidence_path = Path(str(output) + ".verify.evidence.json")
    try:
        publish_run_evidence(evidence_path, evidence)
    except OSError as exc:
        return _domain_failure(
            "verify", "evidence-publish-failed", f"required run evidence could not be published: {exc}", op_id
        )
    _print_json(
        result_envelope(
            "verify",
            "success",
            data={"verified": typed_path(output)},
            evidence=[evidence],
        )
    )
    return 0


def _decide_json(argv: list[str]) -> int:
    parser = _JsonParser(prog="docx2typed decide", add_help=False)
    parser.add_argument(
        "action",
        choices=(
            "accept", "reject", "reinsert", "accept-all", "reject-all", "comment-delete", "apply",
            "table-insert-row", "table-delete-row", "table-insert-col", "table-delete-col",
            "table-merge-cells", "table-split-cells",
        ),
    )
    parser.add_argument("revision_key", nargs="?")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--file", default=None)
    parser.add_argument("--fingerprint", default=None)
    parser.add_argument("--author", default=None)
    parser.add_argument("--text", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--workdir-out", default=None)
    parser.add_argument("--args", default="")
    parser.add_argument("--discard-content", action="store_true")
    parser.add_argument("--operation-id", default=None)
    try:
        args = parser.parse_args(argv)
    except _InvocationError as exc:
        return _invocation_failure("decide", exc.message, argv)

    action = args.action
    if action.startswith("table-"):
        if not args.revision_key or not args.output or not args.workdir_out:
            return _invocation_failure("decide", "table ops need table ref (revision_key), --output and --workdir-out", argv)
    elif action == "comment-delete":
        if not args.revision_key:
            return _invocation_failure("decide", "comment id is required for comment-delete", argv)
    elif action == "apply":
        if not args.file:
            return _invocation_failure("decide", "--file is required for apply (review-decisions.json)", argv)
    elif action in ("accept", "reject", "reinsert"):
        if not args.revision_key or not args.fingerprint:
            return _invocation_failure("decide", "revision_key and --fingerprint are required for accept/reject/reinsert", argv)
    elif action in ("accept-all", "reject-all"):
        if not args.output or not args.workdir_out:
            return _invocation_failure("decide", "--output and --workdir-out are required for accept-all/reject-all", argv)

    from .decisions import (
        _apply_decisions_file,
        _apply_table_op,
        _decide_all,
        _decide_single,
        _delete_comment,
    )

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        return _domain_failure("decide", "workdir-not-found", f"typed workdir not found: {workdir}", args.operation_id)

    new_artifact = action.startswith("table-") or action in ("accept-all", "reject-all")
    if new_artifact:
        output_path = Path(args.output).resolve()
        new_workdir = Path(args.workdir_out).resolve()
        if output_path.exists() or new_workdir.exists():
            return _domain_failure(
                "decide",
                "output-already-exists",
                f"decided output/workdir already exists: {output_path} / {new_workdir}",
                args.operation_id,
            )
        anchor = new_workdir
        directory = True
        evidence_path = new_workdir / "run.evidence.json"
    else:
        anchor = workdir
        directory = True
        evidence_path = workdir / "run.evidence.json"

    manifest_before = _workdir_manifest_sha256(workdir)
    canonical_args = {
        "workdir": str(workdir),
        "action": action,
        "revision_key": args.revision_key,
        "file": args.file,
        "fingerprint": args.fingerprint,
        "author": args.author,
        "text": args.text,
        "output": args.output,
        "workdir_out": args.workdir_out,
        "args": args.args,
        "discard_content": args.discard_content,
    }

    def run():
        if action.startswith("table-"):
            numbers = [int(part) for part in args.args.split() if part.strip().isdigit()]
            created = _apply_table_op(
                workdir, args.revision_key, action[len("table-"):], numbers,
                Path(args.output), Path(args.workdir_out),
                discard_content=args.discard_content,
            )
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {
                    "docx": {"sha256": file_sha256(Path(args.output).resolve())},
                    "workdir": {"manifest_sha256": _workdir_manifest_sha256(created)},
                },
                "action": action,
                "table": args.revision_key,
                "checks": [{"name": "table-op", "status": "pass"}],
            }
            return (
                "success",
                {"operation": action, "table": args.revision_key, "workdir": typed_path(created)},
                "mutation",
                payload,
                [],
            )
        if action == "comment-delete":
            decision = _delete_comment(workdir, args.revision_key)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(workdir)}},
                "decision": {"action": "comment-delete", "comment_id": decision["comment_id"]},
                "checks": [{"name": "comment-delete", "status": "pass"}],
            }
            return "success", {"decision": decision, "state": "clean"}, "mutation", payload, []
        if action == "apply":
            report = _apply_decisions_file(workdir, Path(args.file), include_results=True)
            results = report["results"]
            published = [r for r in results if r["status"] == "published"]
            failed = [r for r in results if r["status"] == "failed"]
            not_attempted = [r for r in results if r["status"] == "not_attempted"]
            data = {
                "applied": len(published),
                "skipped": len(not_attempted),
                "errors": len(failed),
                "published": [{"revision_key": r["revision_key"], "decision": r["decision"]} for r in published],
                "failed": [{"revision_key": r["revision_key"], "reason": r.get("error") or ""} for r in failed],
                "not_attempted": [{"revision_key": r["revision_key"], "decision": r["decision"]} for r in not_attempted],
            }
            diagnostics = [
                diagnostic(
                    "decision-apply-failed",
                    r.get("error") or "entry failed to apply",
                    details={"revision_key": r["revision_key"]},
                )
                for r in failed
            ]
            if published and (failed or not_attempted):
                outcome, kind = "partial", "partial-decision"
            elif failed or not_attempted:
                outcome, kind = "failure", "partial-decision"
            else:
                outcome, kind = "success", "mutation"
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(workdir)}},
                "published": [{"revision_key": r["revision_key"], "decision": r["decision"]} for r in published],
                "failed": [{"revision_key": r["revision_key"], "reason": r.get("error") or ""} for r in failed],
                "not_attempted": [{"revision_key": r["revision_key"], "decision": r["decision"]} for r in not_attempted],
                "checks": [{"name": "apply", "status": outcome}],
            }
            return outcome, data, kind, payload, diagnostics
        if action in ("accept-all", "reject-all"):
            created = _decide_all(
                workdir, "accept" if action == "accept-all" else "reject",
                Path(args.output), Path(args.workdir_out),
            )
            report = json.loads((created / "decisions.json").read_text(encoding="utf-8"))
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {
                    "docx": {"sha256": file_sha256(Path(args.output).resolve())},
                    "workdir": {"manifest_sha256": _workdir_manifest_sha256(created)},
                },
                "action": action,
                "revision_count": report["revision_count"],
                "checks": [{"name": "decide-all", "status": "pass"}],
            }
            return (
                "success",
                {
                    "action": action,
                    "workdir": typed_path(created),
                    "output": typed_path(Path(args.output).resolve()),
                    "revision_count": report["revision_count"],
                },
                "mutation",
                payload,
                [],
            )
        decision = _decide_single(
            workdir, args.revision_key, action=action,
            author=args.author, text=args.text,
            expected_fingerprint=args.fingerprint,
        )
        payload = {
            **base_evidence_payload(),
            "inputs": {"workdir": {"manifest_sha256": manifest_before}},
            "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(workdir)}},
            "decision": {
                "action": action,
                "w_id": decision["w_id"],
                "paragraph_id": decision["paragraph_id"],
                "operation": decision["operation"],
            },
            "checks": [{"name": "decision-published", "status": "pass"}],
        }
        return "success", {"decision": decision, "state": "clean"}, "mutation", payload, []

    try:
        return _run_json_operation(
            "decide",
            operation_id=args.operation_id,
            canonical_args=canonical_args,
            anchor=anchor,
            directory=directory,
            evidence_path=evidence_path,
            run=run,
        )
    except (OSError, zipfile.BadZipFile, TypedError, KeyError, ValueError) as exc:
        code = "workdir-unreadable" if isinstance(exc, OSError) else "workdir-invalid"
        return _domain_failure("decide", code, str(exc), args.operation_id)


_JSON_COMMANDS = {
    "validate": _validate_json,
    "extract": _extract_json,
    "edit": _edit_json,
    "build": _build_json,
    "verify": _verify_json,
    "decide": _decide_json,
}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    json_mode = "--json" in argv
    argv = [arg for arg in argv if arg != "--json"]
    if argv == ["--version"]:
        descriptor = engine_descriptor()
        if json_mode:
            _print_json(descriptor)
        else:
            print(f"{descriptor['name']} {descriptor['version']} ({descriptor['build_commit']})")
        return 0
    if not argv:
        print(__doc__)
        return 1
    command = argv[0]
    if json_mode and command in _JSON_COMMANDS:
        return _JSON_COMMANDS[command](argv[1:])
    if json_mode and command not in ("mcp", "review"):
        return _invocation_failure(
            command,
            f"no Protocol-major-1 --json contract for command: {command}",
            argv,
        )
    if command == "mcp":
        try:
            from .mcp_server import main as mcp_main
        except ImportError:
            from mcp_server import main as mcp_main
        mcp_main()
        return 0
    if command == "review":
        try:
            from .review_server import main as review_main
        except ImportError:
            from review_server import main as review_main
        return review_main(argv[1:])
    commands = {
        "extract": extract,
        "view": view,
        "validate": validate,
        "build": build,
        "verify": verify,
        "normalize": normalize,
        "audit": audit,
        "edit": edit,
        "decide": decide,
    }
    if command in commands:
        return commands[command](argv[1:])
    print(f"Unknown command: {command}\n{__doc__}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
