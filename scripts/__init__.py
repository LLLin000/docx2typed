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
    from .inspect_migrate import (
        MANIFEST_FILE,
        MANIFEST_VERSION,
        WORKDIR_MANIFEST_SCHEMA,
        MigrateError,
        inspect,
        inspect_workdir,
        inventory_assets,
        inventory_sha256,
        migrate,
        migrate_workdir,
    )
except ImportError:
    from inspect_migrate import (
        MANIFEST_FILE,
        MANIFEST_VERSION,
        WORKDIR_MANIFEST_SCHEMA,
        MigrateError,
        inspect,
        inspect_workdir,
        inventory_assets,
        inventory_sha256,
        migrate,
        migrate_workdir,
    )

try:
    from .protocol import (
        EVIDENCE_SCHEMA,
        base_evidence_payload,
        canonical_operation_input,
        derived_workdir_manifest,
        diagnostic,
        domain_diagnostic,
        engine_descriptor,
        file_sha256,
        new_operation_id,
        operation_ledger,
        operation_ledger_path,
        publish_run_evidence,
        result_envelope,
        run_evidence,
        semantic_sha256,
        typed_path,
    )
except ImportError:
    from protocol import (
        EVIDENCE_SCHEMA,
        base_evidence_payload,
        canonical_operation_input,
        derived_workdir_manifest,
        diagnostic,
        domain_diagnostic,
        engine_descriptor,
        file_sha256,
        new_operation_id,
        operation_ledger,
        operation_ledger_path,
        publish_run_evidence,
        result_envelope,
        run_evidence,
        semantic_sha256,
        typed_path,
    )


try:
    from .store import NeedsRecovery, Store, StoreError, has_store, pending_recovery
except ImportError:  # direct script execution has no package context.
    from store import NeedsRecovery, Store, StoreError, has_store, pending_recovery  # type: ignore[no-redef]


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


def _run_before_success(
    before_success: Callable[[], None], operation: str, op_id: str
) -> int | None:
    """Run the pre-success hook (e.g. birth the store for a fresh extract).
    A hook failure emits exactly ONE failure envelope and suppresses the
    success envelope; ``None`` means the success may be printed."""
    try:
        before_success()
    except (StoreError, OSError) as exc:
        code = getattr(exc, "code", None) or "workdir-unreadable"
        _print_json(
            result_envelope(
                operation,
                "failure",
                data={"operation_id": op_id},
                diagnostics=[diagnostic(code, str(exc))],
            )
        )
        return 1
    return None


class _EvidencePublishError(OSError):
    """Required run-evidence sidecar could not be published during recovery.

    Distinct from plain OSError (which callers map to workdir-unreadable):
    recovery callers catch this first and report ``evidence-publish-failed``
    with the ledger left pending so a retry can repair the sidecar."""


def _evidence_publish_failure(operation: str, operation_id: str | None, detail: str) -> int:
    _print_json(
        result_envelope(
            operation,
            "failure",
            data={"operation_id": operation_id},
            diagnostics=[
                diagnostic(
                    "evidence-publish-failed",
                    f"required run evidence could not be published: {detail}",
                )
            ],
        )
    )
    return 1


def _ledger_invalid_failure(
    operation: str, operation_id: str, ledger_path: Path
) -> int:
    """Structured ``operation-ledger-invalid`` failure for a corrupt persisted
    row: the effect may have completed, so the operation must not rerun or
    reconstruct; the corrupt row is preserved for inspection."""
    _print_json(
        result_envelope(
            operation,
            "failure",
            data={"operation_id": operation_id},
            diagnostics=[
                domain_diagnostic(
                    "operation-ledger-invalid",
                    f"ledger record for operation_id {operation_id!r} is corrupt; "
                    f"repair or remove {ledger_path}",
                )
            ],
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
        recovery = pending_recovery(argv[0])
        data = {
            "valid": True,
            "workdir": typed_path(checked.path),
            "warnings": checked.warnings,
        }
        if recovery:
            data["recovery"] = recovery
        _print_json(result_envelope("validate", "success", data=data))
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
    run: Callable[[Path], tuple[str, dict[str, Any], str, dict[str, Any], list[dict[str, Any]]]],
    store_workdir: Path | None = None,
    store_generation: bool = True,
    before_success: Callable[[], None] | None = None,
) -> int:
    """Execute one finite operation under the Result/Evidence/Operation-ID
    contract. ``run(target)`` returns (outcome, data, kind, payload,
    diagnostics) or raises _DomainFailure (mapped, exit 1). Success and
    partial outcomes publish run evidence; a publish failure can never report
    success. Identical retries replay the original envelope; changed canonical
    input fails ``operation-id-reused`` with no second effect.

    With ``store_workdir`` the mutation runs through the immutable-generation
    store (Writer lane, CAS, durable journals, startup recovery, atomic
    external publication) and ``run`` receives the fresh generation directory
    (or the pinned generation for external-only publication).

    ``before_success`` (optional) runs after the effect landed but before any
    success envelope is printed; a hook failure emits ONE failure envelope
    and suppresses the success (no success-first double emit)."""
    op_id = operation_id if operation_id else new_operation_id()
    canonical = canonical_operation_input(operation, canonical_args)
    store = None
    if store_workdir is not None:
        if not has_store(store_workdir):
            # Lazy upgrade of a pre-store workdir: birth generation 0 from the
            # current root files before the mutation routes through the store.
            try:
                Store.ensure(store_workdir, operation_id=op_id, input_sha256=canonical)
            except (StoreError, OSError) as exc:
                code = getattr(exc, "code", None) or "workdir-unreadable"
                return _domain_failure(operation, code, str(exc), op_id)
        try:
            store = Store.open(store_workdir)
        except (StoreError, OSError) as exc:
            code = getattr(exc, "code", None) or "workdir-unreadable"
            return _domain_failure(operation, code, str(exc), op_id)
    ledger_anchor = anchor
    if store is not None:
        # Replay lookup must hit the generation the record was written under:
        # the pointer may have advanced past the committing generation, so
        # search every generation, not just the current pin.
        record, corrupt_path = store.lookup_ledger(
            op_id, generation=store_generation, anchor=anchor, directory=directory
        )
    else:
        record = operation_ledger.lookup_persisted(op_id, ledger_anchor, directory=directory)
        corrupt_path = None
        if record is None:
            corrupt_path = operation_ledger.corrupt_persisted(
                op_id, ledger_anchor, directory=directory
            )
    if record is None and corrupt_path is not None:
        # Corrupt persisted row for this operation_id: the effect may have
        # completed (e.g. a lost pending marker), so never rerun. Fail closed
        # with a structured Result naming the exact ledger file; the corrupt
        # row stays for inspection.
        return _ledger_invalid_failure(operation, op_id, corrupt_path)
    if record is not None:
        if record["input_sha256"] == canonical:
            envelope = record.get("envelope")
            if isinstance(envelope, dict) and envelope.get("outcome") in (
                "success",
                "failure",
                "partial",
            ):
                if before_success is not None and envelope["outcome"] == "success":
                    hook_failure = _run_before_success(before_success, operation, op_id)
                    if hook_failure is not None:
                        return hook_failure
                _print_json(envelope)
                return 0 if envelope["outcome"] == "success" else 1
            # Missing/pending envelope: the operation never completed. Fall
            # through and rerun the idempotent operation (records are shape-
            # validated at read time, so a corrupt record can never replay).
        else:
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
    if store is not None:
        return _run_store_mutation(
            operation,
            op_id,
            canonical,
            store,
            run,
            evidence_path,
            generation=store_generation,
            anchor=anchor,
            directory=directory,
        )
    try:
        outcome, data, kind, payload, diagnostics = run(Path(anchor))
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
                    # Exception class + stable evidence path only: the raw
                    # OSException text embeds the transient mkstemp temp
                    # filename, so it is never serialized into the envelope
                    # (identical retries must be byte-identical).
                    f"required run evidence could not be published: {type(exc).__name__}: {evidence_path}",
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
    if before_success is not None and outcome == "success":
        hook_failure = _run_before_success(before_success, operation, op_id)
        if hook_failure is not None:
            return hook_failure
    _print_json(envelope)
    return 0 if outcome == "success" else 1


def _run_store_mutation(
    operation: str,
    op_id: str,
    canonical: str,
    store: "Store",
    run: Callable[..., tuple[str, dict[str, Any], str, dict[str, Any], list[dict[str, Any]]]],
    evidence_path: Path,
    *,
    generation: bool,
    anchor: Path,
    directory: bool,
) -> int:
    """Run one mutation through the immutable-generation store and publish the
    committed envelope. The store owns evidence, ledger, journal, pointer, and
    external publication durability; the caller only prints the envelope."""
    try:
        pin = store.pin()
        expected_generation = pin["generation"]
        expected_manifest = pin["manifest_sha256"]

        def adapter(target: Path, tx: Any) -> tuple[Any, ...]:
            result = run(target, tx)
            outcome, data, kind, payload, diagnostics = result
            return outcome, data, kind, payload, diagnostics

        envelope = store.mutate(
            operation=operation,
            operation_id=op_id,
            canonical=canonical,
            input_sha256=expected_manifest or canonical,
            expected_generation=expected_generation,
            run=adapter,
            generation=generation,
            ledger_anchor=None if generation else anchor,
            ledger_directory=directory if not generation else True,
            evidence_path=None if generation else evidence_path,
        )
    except NeedsRecovery as exc:
        return _domain_failure(operation, exc.code, str(exc), op_id)
    except StoreError as exc:
        return _domain_failure(operation, exc.code, str(exc), op_id)
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
    except (OSError, zipfile.BadZipFile, TypedError, KeyError, ValueError) as exc:
        code = "workdir-unreadable" if isinstance(exc, OSError) else "workdir-invalid"
        return _domain_failure(operation, code, str(exc), op_id)
    _print_json(envelope)
    return 0 if envelope["outcome"] == "success" else 1


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

    def run(target):
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

    def birth_store() -> None:
        """Birth the immutable-generation store for the new workdir AFTER the
        assets exist but BEFORE the success envelope is printed: a failed
        init emits exactly one failure envelope, never success-then-failure."""
        if has_store(anchor):
            return
        Store.init(
            anchor,
            operation_id=args.operation_id or new_operation_id(),
            input_sha256=canonical_args["source_sha256"],
        )

    return _run_json_operation(
        "extract",
        operation_id=args.operation_id,
        canonical_args=canonical_args,
        anchor=anchor,
        directory=True,
        evidence_path=anchor / "run.evidence.json",
        run=run,
        before_success=birth_store,
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

        def run(target, tx=None):
            try:
                state_path = refresh_edit_projection(target, init=args.init, discard=args.discard)
            except (OSError, zipfile.BadZipFile, TypedError) as exc:
                code = "workdir-unreadable" if isinstance(exc, OSError) else "workdir-invalid"
                raise _DomainFailure(diagnostic(code, str(exc))) from exc
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "edit-refresh", "status": "pass"}],
            }
            return "success", {"refreshed": True, "edit_state": typed_path(state_path)}, "mutation", payload, []
    else:
        canonical_args["track"] = "track" if args.track else ("no-track" if args.no_track else None)
        canonical_args["author"] = args.author
        track: bool | None = True if args.track else (False if args.no_track else None)

        def run(target, tx=None):
            try:
                state_path, warnings, changed_ids = sync_edit_projection(
                    target, track=track, author=args.author
                )
            except (OSError, zipfile.BadZipFile, TypedError) as exc:
                code = "workdir-unreadable" if isinstance(exc, OSError) else "workdir-invalid"
                raise _DomainFailure(diagnostic(code, str(exc))) from exc
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
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
        store_workdir=workdir,
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

    def run(target, tx=None):
        try:
            if tx is not None:
                # Journaled external publication: build into store staging,
                # register the target, and let the store publish atomically
                # (prior output backed up; recovery rolls forward/back).
                staged = tx.staging("build.docx")
                built = build_workdir(target, staged)
                tx.stage_external(output, staged, mode="replace")
            else:
                built = build_workdir(target, args.output)
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
        published = output if tx is not None else built
        return "success", {"output": typed_path(published)}, "build", payload, []

    return _run_json_operation(
        "build",
        operation_id=args.operation_id,
        canonical_args=canonical_args,
        anchor=output,
        directory=False,
        evidence_path=Path(str(output) + ".evidence.json"),
        run=run,
        store_workdir=workdir,
        store_generation=False,
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
            "verify",
            "evidence-publish-failed",
            f"required run evidence could not be published: {type(exc).__name__}: {evidence_path}",
            op_id,
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

    def run(target, tx=None):
        if action.startswith("table-"):
            numbers = [int(part) for part in args.args.split() if part.strip().isdigit()]
            if tx is not None:
                output_staged = tx.staging("decided.docx")
                created = _apply_table_op(
                    target, args.revision_key, action[len("table-"):], numbers,
                    output_staged, Path(args.workdir_out),
                    discard_content=args.discard_content,
                )
                tx.stage_external(Path(args.output).resolve(), output_staged, mode="create")
                # The final path does not exist yet: publish happens after the
                # prepared journal. Hash the staged artifact and record the
                # final path in the evidence payload.
                output_real = Path(args.output).resolve()
                docx_evidence = {"sha256": file_sha256(output_staged), "path": str(output_real)}
            else:
                created = _apply_table_op(
                    target, args.revision_key, action[len("table-"):], numbers,
                    Path(args.output), Path(args.workdir_out),
                    discard_content=args.discard_content,
                )
                output_real = Path(args.output).resolve()
                docx_evidence = {"sha256": file_sha256(output_real)}
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {
                    "docx": docx_evidence,
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
            decision = _delete_comment(target, args.revision_key)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "decision": {"action": "comment-delete", "comment_id": decision["comment_id"]},
                "checks": [{"name": "comment-delete", "status": "pass"}],
            }
            return "success", {"decision": decision, "state": "clean"}, "mutation", payload, []
        if action == "apply":
            report = _apply_decisions_file(target, Path(args.file), include_results=True)
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
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "published": [{"revision_key": r["revision_key"], "decision": r["decision"]} for r in published],
                "failed": [{"revision_key": r["revision_key"], "reason": r.get("error") or ""} for r in failed],
                "not_attempted": [{"revision_key": r["revision_key"], "decision": r["decision"]} for r in not_attempted],
                "checks": [{"name": "apply", "status": outcome}],
            }
            return outcome, data, kind, payload, diagnostics
        if action in ("accept-all", "reject-all"):
            if tx is not None:
                output_staged = tx.staging("decided.docx")
                created = _decide_all(
                    target, "accept" if action == "accept-all" else "reject",
                    output_staged, Path(args.workdir_out),
                )
                tx.stage_external(Path(args.output).resolve(), output_staged, mode="create")
                output_hashed = output_staged
                output_real = Path(args.output).resolve()
            else:
                created = _decide_all(
                    target, "accept" if action == "accept-all" else "reject",
                    Path(args.output), Path(args.workdir_out),
                )
                output_hashed = Path(args.output).resolve()
                output_real = output_hashed
            report = json.loads((created / "decisions.json").read_text(encoding="utf-8"))
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {
                    "docx": {"sha256": file_sha256(output_hashed), "path": str(output_real)},
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
                    "output": typed_path(output_real),
                    "revision_count": report["revision_count"],
                },
                "mutation",
                payload,
                [],
            )
        decision = _decide_single(
            target, args.revision_key, action=action,
            author=args.author, text=args.text,
            expected_fingerprint=args.fingerprint,
        )
        payload = {
            **base_evidence_payload(),
            "inputs": {"workdir": {"manifest_sha256": manifest_before}},
            "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
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
            store_workdir=workdir,
            store_generation=not new_artifact,
        )
    except (OSError, zipfile.BadZipFile, TypedError, KeyError, ValueError) as exc:
        code = "workdir-unreadable" if isinstance(exc, OSError) else "workdir-invalid"
        return _domain_failure("decide", code, str(exc), args.operation_id)


def _reconstructed_evidence_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the migrate evidence payload from the published manifest.

    Used only when the evidence sidecar was lost after a completed publish;
    the primary recovery path replays the exact published sidecar record.
    Callers must have validated ``manifest`` first (``_published_manifest_ok``),
    which guarantees every key accessed here exists with the required type."""
    opaque = [
        asset
        for asset in manifest["assets"]
        if asset.get("kind") == "opaque" and asset.get("presence") == "present"
    ]
    return {
        **base_evidence_payload(),
        "inputs": {
            "source": {
                "inventory_sha256": manifest["source"]["identity"],
                "semantic_manifest_sha256": manifest["source"][
                    "semantic_manifest_sha256"
                ],
            }
        },
        "outputs": {
            "target": {
                "manifest_sha256": semantic_sha256(manifest),
                "semantic_manifest_sha256": manifest["state"][
                    "semantic_manifest_sha256"
                ],
                "assets": len(manifest["assets"]),
                "opaque_assets": len(opaque),
            }
        },
        "checks": manifest["checks"],
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _published_manifest_ok(
    manifest: dict[str, Any],
    operation_id: str,
    source_identity: str,
) -> bool:
    """Structural/type/identity validation of a published workdir manifest.

    Confirms the schema/version identity, producer operation and
    operation_id, source identity, the exact generated self-entry invariant
    (one ``workdir.manifest.json`` entry with null hash/bytes/mtime), and
    well-formed non-generated entries (real path, kind, presence, 64-hex
    sha256, non-negative byte count). Purely structural: actual target
    hashes are checked separately by ``_target_closure_matches``. Never
    raises on malformed nested values; returns False instead, so
    reconstruction can fail closed."""
    if not isinstance(manifest, dict):
        return False
    if manifest.get("schema") != WORKDIR_MANIFEST_SCHEMA:
        return False
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        return False
    producer = manifest.get("producer")
    if not isinstance(producer, dict):
        return False
    if producer.get("operation") != "migrate":
        return False
    if producer.get("operation_id") != operation_id:
        return False
    source = manifest.get("source")
    if not isinstance(source, dict):
        return False
    if source.get("identity") != source_identity:
        return False
    if not _is_sha256(source.get("semantic_manifest_sha256")):
        return False
    state = manifest.get("state")
    if not isinstance(state, dict) or not _is_sha256(
        state.get("semantic_manifest_sha256")
    ):
        return False
    if not isinstance(manifest.get("checks"), list):
        return False
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        return False
    generated = [
        asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("kind") == "generated"
    ]
    if len(generated) != 1:  # the manifest declares exactly one generated asset
        return False
    self_entry = generated[0]
    if (
        self_entry.get("path") != MANIFEST_FILE
        or self_entry.get("role") != "workdir-manifest"
        or self_entry.get("required") is not True
        or self_entry.get("read_only") is not True
        or self_entry.get("presence") != "present"
        or self_entry.get("bytes") is not None
        or self_entry.get("sha256") is not None
        or self_entry.get("mtime_ns") is not None
    ):
        return False
    for asset in assets:
        if asset is self_entry:
            continue
        if not isinstance(asset, dict):
            return False
        if not isinstance(asset.get("path"), str):
            return False
        if asset.get("kind") not in ("authoritative", "optional", "opaque"):
            return False
        if asset.get("presence") != "present":
            return False
        if not _is_sha256(asset.get("sha256")):
            return False
        if not isinstance(asset.get("bytes"), int) or asset["bytes"] < 0:
            return False
        if not isinstance(asset.get("required"), bool) or not isinstance(
            asset.get("read_only"), bool
        ):
            return False
    return True


def _target_closure_matches(manifest: dict[str, Any], target: Path) -> bool:
    """The actual target asset closure/hashes equal the manifest declaration.

    Every declared non-generated present asset must exist under ``target``
    with the declared byte count and sha256, and no undeclared file may be
    present (``workdir.manifest.json`` is the sole generated exception).
    May raise OSError when the target cannot be inventoried; callers map
    that to a workdir-unreadable Result."""
    declared = {
        asset["path"]: asset
        for asset in manifest["assets"]
        if asset.get("kind") != "generated"
    }
    actual = {
        asset["path"]: asset
        for asset in inventory_assets(target)
        if asset["presence"] == "present" and asset["path"] != MANIFEST_FILE
    }
    if set(declared) != set(actual):
        return False
    for path, asset in declared.items():
        found = actual[path]
        if found["sha256"] != asset["sha256"] or found["bytes"] != asset["bytes"]:
            return False
    return True


def _replayable_evidence(
    candidate: Any,
    operation_id: str,
    canonical_payload: dict[str, Any],
) -> bool:
    """True only for an exact published migrate evidence sidecar.

    Replay requires the frozen ``docx2typed-run-evidence-1`` shape for
    migrate/success/mutation, the matching operation_id, a payload equal to
    the canonical payload rebuilt from the validated manifest, and a
    payload_sha256 that actually covers that payload. Anything else is
    ignored and deterministically rebuilt."""
    if not isinstance(candidate, dict):
        return False
    if candidate.get("schema") != EVIDENCE_SCHEMA:
        return False
    if candidate.get("operation") != "migrate":
        return False
    if candidate.get("outcome") != "success":
        return False
    if candidate.get("kind") != "mutation":
        return False
    if candidate.get("operation_id") != operation_id:
        return False
    payload = candidate.get("payload")
    if not isinstance(payload, dict) or payload != canonical_payload:
        return False
    if candidate.get("payload_sha256") != semantic_sha256(payload):
        return False
    return True


def _validated_published_manifest(
    operation_id: str,
    source: Path,
    target: Path,
) -> dict[str, Any] | None:
    """The published manifest after full validation against the actual source
    and target, or None when anything fails.

    Validation covers structural/type identity (``_published_manifest_ok``),
    the RECOMPUTED source and target semantic manifest identities (an altered
    metadata-only manifest — including a tampered stored hash that still
    shape-checks — fails reconstruction), and the actual target asset
    closure/hashes (``_target_closure_matches``). Raises OSError when the
    source or target cannot be inventoried.
    """
    manifest_path = target / MANIFEST_FILE
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    source_identity = inventory_sha256(source)
    if not _published_manifest_ok(manifest, operation_id, source_identity):
        return None
    if (
        semantic_sha256(derived_workdir_manifest(source))
        != manifest["source"]["semantic_manifest_sha256"]
    ):
        return None
    if (
        semantic_sha256(derived_workdir_manifest(target))
        != manifest["state"]["semantic_manifest_sha256"]
    ):
        return None
    if not _target_closure_matches(manifest, target):
        return None
    return manifest


def _reconstruct_migrate_success(
    operation_id: str,
    source: Path,
    target: Path,
) -> dict[str, Any] | None:
    """Best-effort reconstruction of a published migrate Result.

    Closes the replay gap after the atomic publish but before the success
    record landed: when the target manifest proves this operation's publish
    (``_validated_published_manifest``: producer.operation_id and
    source.identity match, manifest shape and self-entry invariants hold,
    recomputed semantic identities match, and the actual target asset
    closure/hashes equal the declaration), the migration demonstrably
    completed, so the original Result is returned instead of failing with
    ``target-already-exists``. The evidence sidecar is replayed only when it
    is a valid exact record for this publish; an invalid or lost sidecar is
    ignored and deterministically rebuilt from the validated manifest.
    Returns None when the target is not this operation's publish or the
    manifest/target was tampered with. Raises OSError when the source or
    target cannot be inventoried.
    """
    manifest = _validated_published_manifest(operation_id, source, target)
    if manifest is None:
        return None
    evidence_path = Path(str(target) + ".migrate.evidence.json")
    canonical_payload = _reconstructed_evidence_payload(manifest)
    try:
        candidate = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        candidate = None
    if not _replayable_evidence(candidate, operation_id, canonical_payload):
        # Invalid or lost sidecar: rebuild deterministically from the
        # validated manifest, then repair the sidecar in place so a later
        # replay finds a valid record. The rebuild publish is REQUIRED: a
        # recovered success without a durable sidecar would violate the
        # evidence contract, so a failed write raises _EvidencePublishError
        # and the caller reports evidence-publish-failed with the ledger
        # left pending (a retry can repair).
        evidence = run_evidence(
            "migrate",
            "success",
            kind="mutation",
            operation_id=operation_id,
            payload=canonical_payload,
        )
        try:
            publish_run_evidence(evidence_path, evidence)
        except OSError as exc:
            # Exception class + stable evidence path only: the raw OSException
            # text embeds the transient mkstemp temp filename, so it is never
            # carried into the recovery failure envelope (retries must be
            # byte-identical).
            raise _EvidencePublishError(f"{type(exc).__name__}: {evidence_path}") from exc
    else:
        evidence = candidate
    return result_envelope(
        "migrate",
        "success",
        data={
            "operation_id": operation_id,
            "workdir": typed_path(target),
            "manifest": typed_path(target / MANIFEST_FILE),
        },
        evidence=[evidence],
    )


def _repair_evidence_sidecar(
    evidence_path: Path, evidence: dict[str, Any]
) -> bool:
    """Repair the external evidence sidecar from the stored exact envelope
    when it is missing or does not match. Returns True when the sidecar
    already matches or the repair publish succeeded; False when the required
    sidecar could not be written — the caller must then report an
    ``evidence-publish-failed`` Result and keep the ledger pending so a retry
    can repair. Never returns recovered success on a failed write."""
    try:
        candidate = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        candidate = None
    if candidate == evidence:
        return True
    try:
        publish_run_evidence(evidence_path, evidence)
    except OSError:
        return False
    return True


def _inspect_json(argv: list[str]) -> int:
    parser = _JsonParser(prog="docx2typed inspect", add_help=False)
    parser.add_argument("source", help="schema-1 typed workdir")
    try:
        args = parser.parse_args(argv)
    except _InvocationError as exc:
        return _invocation_failure("inspect", exc.message, argv)

    source = Path(args.source).resolve()
    if not source.exists():
        return _domain_failure("inspect", "workdir-not-found", f"source workdir not found: {source}", None)
    if not source.is_dir():
        return _domain_failure("inspect", "workdir-invalid", f"source is not a directory: {source}", None)
    try:
        data = inspect_workdir(source)
    except (PermissionError, OSError) as exc:
        return _domain_failure("inspect", "workdir-unreadable", str(exc), None)
    _print_json(result_envelope("inspect", "success", data=data))
    return 0


def _migrate_json(argv: list[str]) -> int:
    parser = _JsonParser(prog="docx2typed migrate", add_help=False)
    parser.add_argument("source", help="schema-1 typed workdir (never modified)")
    parser.add_argument("--out", required=True, help="target manifest-backed workdir")
    parser.add_argument("--operation-id", default=None, help="retry identity")
    try:
        args = parser.parse_args(argv)
    except _InvocationError as exc:
        return _invocation_failure("migrate", exc.message, argv)

    source = Path(args.source).resolve()
    target = Path(args.out).resolve()
    if not args.operation_id:
        return _domain_failure(
            "migrate",
            "operation-id-required",
            "migrate requires --operation-id in the Protocol JSON contract",
            None,
        )
    op_id = args.operation_id
    if not source.exists():
        return _domain_failure("migrate", "workdir-not-found", f"source workdir not found: {source}", op_id)
    if not source.is_dir():
        return _domain_failure("migrate", "workdir-invalid", f"source is not a directory: {source}", op_id)

    # Inventory/canonical computation happens inside the JSON error
    # translation: an unreadable source emits a workdir-unreadable Result
    # instead of an uncaught OSError.
    try:
        canonical = canonical_operation_input(
            "migrate",
            {
                "source": str(source),
                "out": str(target),
                "source_inventory_sha256": inventory_sha256(source),
            },
        )
    except (PermissionError, OSError) as exc:
        return _domain_failure("migrate", "workdir-unreadable", str(exc), op_id)

    ledger_file = Path(str(target) + ".operation-ledger.json")
    evidence_path = Path(str(target) + ".migrate.evidence.json")
    record = operation_ledger.lookup_file(op_id, ledger_file)
    if record is None and operation_ledger.corrupt_file(op_id, ledger_file):
        # Corrupt persisted row for this operation_id: the migration may have
        # completed (e.g. a lost pending marker), so never rerun, never
        # reconstruct. Fail closed; the corrupt row stays for inspection.
        return _ledger_invalid_failure("migrate", op_id, ledger_file)
    if record is not None:
        if record["input_sha256"] != canonical:
            _print_json(
                result_envelope(
                    "migrate",
                    "failure",
                    data={"operation_id": op_id},
                    diagnostics=[
                        diagnostic(
                            "operation-id-reused",
                            f"operation_id {op_id!r} was already used with different canonical input",
                        )
                    ],
                )
            )
            return 1
        envelope = record.get("envelope")
        pending = record.get("pending") is True or envelope is None
        if envelope is not None and not pending:
            _print_json(envelope)
            return 0 if envelope["outcome"] == "success" else 1
        if envelope is not None:
            # Pending record carrying the prepared exact envelope: replay it
            # ONLY after validating the published target/manifest against the
            # source; never claim success without a proven publish.
            try:
                manifest = _validated_published_manifest(op_id, source, target)
            except (PermissionError, OSError) as exc:
                return _domain_failure("migrate", "workdir-unreadable", str(exc), op_id)
            if manifest is not None:
                canonical_payload = _reconstructed_evidence_payload(manifest)
                stored_evidence = envelope.get("evidence") or []
                if (
                    len(stored_evidence) == 1
                    and _replayable_evidence(
                        stored_evidence[0], op_id, canonical_payload
                    )
                ):
                    # Repair the external sidecar from the stored exact
                    # envelope, then upgrade the record without changing the
                    # envelope: the response stays byte-exact. A repair
                    # failure keeps the record pending and fails the replay —
                    # recovered success is never returned without a durable
                    # required sidecar, and the retry can repair.
                    if not _repair_evidence_sidecar(evidence_path, stored_evidence[0]):
                        return _evidence_publish_failure(
                            "migrate", op_id, f"sidecar repair failed: {evidence_path}"
                        )
                    operation_ledger.record_file(
                        op_id, canonical, envelope, ledger_file
                    )
                    _print_json(envelope)
                    return 0
                return _domain_failure(
                    "migrate",
                    "target-already-exists",
                    f"target already exists: {target}",
                    op_id,
                )
            if target.exists():
                return _domain_failure(
                    "migrate",
                    "target-already-exists",
                    f"target already exists: {target}",
                    op_id,
                )
            # No publish landed yet: rerun below; on_prepared refreshes the
            # pending envelope before the new publish.
        else:
            # Pending record without an envelope (pre-publish crash window):
            # replay the publish when it actually landed, otherwise rerun.
            try:
                reconstructed = _reconstruct_migrate_success(op_id, source, target)
            except _EvidencePublishError as exc:
                return _evidence_publish_failure(
                    "migrate", op_id, f"reconstruction sidecar publish failed: {exc}"
                )
            except (PermissionError, OSError) as exc:
                return _domain_failure("migrate", "workdir-unreadable", str(exc), op_id)
            if reconstructed is not None:
                operation_ledger.record_file(op_id, canonical, reconstructed, ledger_file)
                _print_json(reconstructed)
                return 0
    else:
        # No ledger record but the target exists: this is the crash window
        # after the atomic publish and before the success record landed.
        # Replay the publish when the target manifest proves it belongs to
        # this operation_id and source identity; otherwise refuse to touch
        # the existing target.
        if target.exists():
            try:
                reconstructed = _reconstruct_migrate_success(op_id, source, target)
            except _EvidencePublishError as exc:
                return _evidence_publish_failure(
                    "migrate", op_id, f"reconstruction sidecar publish failed: {exc}"
                )
            except (PermissionError, OSError) as exc:
                return _domain_failure("migrate", "workdir-unreadable", str(exc), op_id)
            if reconstructed is not None:
                operation_ledger.record_file(op_id, canonical, reconstructed, ledger_file)
                _print_json(reconstructed)
                return 0
            return _domain_failure("migrate", "target-already-exists", f"target already exists: {target}", op_id)
        # Fresh start: persist the pending record before the first publish
        # attempt so a pre-publish crash leaves a retryable record.
        operation_ledger.record_file(op_id, canonical, None, ledger_file, pending=True)

    prepared: dict[str, Any] | None = None

    def _on_prepared(evidence: dict[str, Any]) -> None:
        """Persist the exact success envelope as the pending record BEFORE
        the atomic publish (invoked by migrate_workdir once the final
        manifest and evidence are known). A crash after the publish therefore
        replays this byte-exact original response."""
        nonlocal prepared
        envelope = result_envelope(
            "migrate",
            "success",
            data={
                "operation_id": op_id,
                "workdir": typed_path(target),
                "manifest": typed_path(target / MANIFEST_FILE),
            },
            evidence=[evidence],
        )
        prepared = envelope
        operation_ledger.record_file(op_id, canonical, envelope, ledger_file, pending=True)

    try:
        migrated, evidence = migrate_workdir(
            source,
            target,
            operation_id=op_id,
            evidence_path=evidence_path,
            on_prepared=_on_prepared,
        )
    except MigrateError as exc:
        operation_ledger.forget_file(op_id, ledger_file)
        return _domain_failure("migrate", exc.code, str(exc), op_id)
    except (PermissionError, OSError) as exc:
        operation_ledger.forget_file(op_id, ledger_file)
        return _domain_failure("migrate", "workdir-unreadable", str(exc), op_id)

    envelope = prepared
    if envelope is None:  # pragma: no cover - on_prepared always fires before publish
        envelope = result_envelope(
            "migrate",
            "success",
            data={
                "operation_id": op_id,
                "workdir": typed_path(migrated),
                "manifest": typed_path(migrated / "workdir.manifest.json"),
            },
            evidence=[evidence],
        )
    # Upgrade pending -> complete without changing the envelope: the first
    # response and every replay share the same byte-exact Result.
    operation_ledger.record_file(op_id, canonical, envelope, ledger_file)
    _print_json(envelope)
    return 0


_JSON_COMMANDS = {
    "validate": _validate_json,
    "extract": _extract_json,
    "inspect": _inspect_json,
    "migrate": _migrate_json,
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
        "inspect": inspect,
        "migrate": migrate,
    }
    if command in commands:
        return commands[command](argv[1:])
    print(f"Unknown command: {command}\n{__doc__}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
