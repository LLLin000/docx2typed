"""docx2typed audit — explicit scan → policy → apply governance for vertical normalization.

Workflow:

    docx2typed audit scan <workdir> -o <scan.json>

    `audit scan` is strictly read-only. It validates the typed workdir and writes
    a scan artifact (schema vertical-normalization-scan-1) that binds every
    vertical-normalization candidate to a deterministic workdir snapshot
    (project, baseline, draft, model, catalog, scanner contract version) and
    fingerprints each candidate. The workdir is never modified.

    A normalization policy (schema vertical-normalization-policy-2) is then
    written against that scan: one explicit convert/preserve decision with an
    actor (and a rationale for risky classifications) for every candidate, plus
    a complete, explicit approval object (human or self). Classification is
    never a decision.

    docx2typed audit apply <workdir> --scan <scan.json> --policy <policy.json> \
        -o <normalized.docx> --workdir-out <normalized-workdir>

    `audit apply` is the only mutation step. It refuses incomplete, stale, or
    unapproved policies before any transformation: the policy must be complete
    (every scan candidate decided), approved (status=approved with an approval
    object), and bound to the exact scan artifact and workdir snapshot. Only
    then does it run the existing normalization transformation, which emits a
    new normalized DOCX and workdir and leaves the original project unchanged.

    There is no auto-apply and no fix-all. Every run writes a run-evidence
    record beside the requested artifact (command, status, timestamps, input
    and output hashes, policy/scan references, and diagnostics).
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .audit_contract import (
        policy_sha256,
        require_approved,
        require_complete,
        run_evidence,
        validate_policy,
        validate_scan_artifact,
    )
    from .typed_core import TypedError
    from .typed_docx import ValidationError, sha256_file
    from .typed_normalize import normalize_workdir, scan_workdir
except ImportError:  # direct script execution has no package context.
    from audit_contract import (
        policy_sha256,
        require_approved,
        require_complete,
        run_evidence,
        validate_policy,
        validate_scan_artifact,
    )
    from typed_core import TypedError
    from typed_docx import ValidationError, sha256_file
    from typed_normalize import normalize_workdir, scan_workdir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: str | Path, what: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read {what}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{what} must be a JSON object")
    return data


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _run_evidence_path(beside: str | Path) -> Path:
    return Path(str(beside) + ".run.json")


def _write_run_evidence(evidence: dict[str, Any], beside: str | Path) -> None:
    _write_json(_run_evidence_path(beside), evidence)


def _safe_write_run_evidence(evidence: dict[str, Any], beside: str | Path) -> None:
    """Write run evidence without masking the primary command result."""
    try:
        _write_run_evidence(evidence, beside)
    except OSError:
        pass


def _snapshot_refs(scan: dict[str, Any]) -> dict[str, str]:
    snapshot = scan["snapshot"]
    return {
        "baseline_sha256": snapshot["baseline_sha256"],
        "draft_snapshot_sha256": snapshot["draft_snapshot_sha256"],
        "model_sha256": snapshot["model_sha256"],
        "catalog_sha256": snapshot["catalog_sha256"],
        "scanner_contract_version": str(snapshot["scanner_contract_version"]),
    }


def _run_scan(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="docx2typed audit scan")
    parser.add_argument("workdir", help="typed workdir to scan (read-only)")
    parser.add_argument("-o", "--output", required=True, help="scan artifact JSON to write")
    args = parser.parse_args(argv)
    started = _now()
    output_path = Path(args.output).resolve()
    workdir_path = Path(args.workdir).resolve()
    try:
        scan = scan_workdir(workdir_path)
        _write_json(output_path, scan)
        evidence = run_evidence(
            command=["docx2typed", "audit", "scan", str(workdir_path), "-o", str(output_path)],
            status="ok",
            started_at=started,
            finished_at=_now(),
            inputs={
                "workdir": str(workdir_path),
                **_snapshot_refs(scan),
            },
            outputs={
                "scan_artifact": str(output_path),
                "scan_artifact_sha256": scan["scan_artifact_sha256"],
            },
            scan_artifact_sha256=scan["scan_artifact_sha256"],
            diagnostics=None,
        )
        _safe_write_run_evidence(evidence, output_path)
        print(f"scan: {output_path}")
        print(f"candidates: {len(scan['candidates'])}")
        return 0
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        evidence = run_evidence(
            command=["docx2typed", "audit", "scan", str(workdir_path), "-o", str(output_path)],
            status="error",
            started_at=started,
            finished_at=_now(),
            inputs={"workdir": str(workdir_path)},
            outputs={"scan_artifact": str(output_path)},
            diagnostics=str(exc),
        )
        _safe_write_run_evidence(evidence, output_path)
        print(f"ERROR: {exc}")
        return 1


def _run_apply(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="docx2typed audit apply")
    parser.add_argument("workdir", help="typed workdir to normalize from (left unchanged)")
    parser.add_argument("--scan", required=True, help="scan artifact JSON from `audit scan`")
    parser.add_argument("--policy", required=True, help="complete, approved normalization policy JSON")
    parser.add_argument("-o", "--output", required=True, help="normalized DOCX to write")
    parser.add_argument("--workdir-out", required=True, help="new normalized workdir to create")
    args = parser.parse_args(argv)
    started = _now()
    workdir_path = Path(args.workdir).resolve()
    output_path = Path(args.output).resolve()
    scan_path = Path(args.scan).resolve()
    policy_path = Path(args.policy).resolve()
    scan = None
    policy = None
    try:
        scan = validate_scan_artifact(_load_json(scan_path, "scan artifact"))
        policy = validate_policy(_load_json(policy_path, "normalization policy"), scan=scan)
        policy = require_complete(policy, scan)
        policy = require_approved(policy, scan)
        new_workdir = normalize_workdir(workdir_path, policy_path, output_path, Path(args.workdir_out).resolve(), scan_path=scan_path)
        outputs: dict[str, Any] = {
            "normalized_docx": str(output_path),
            "normalized_docx_sha256": sha256_file(output_path),
            "normalized_workdir": str(Path(new_workdir).resolve()),
        }
        try:  # read-only snapshot of the new workdir for run evidence.
            out_scan = scan_workdir(Path(new_workdir))
            outputs.update({f"normalized_workdir_{key}": value for key, value in _snapshot_refs(out_scan).items()})
        except (OSError, zipfile.BadZipFile, TypedError):
            pass
        evidence = run_evidence(
            command=[
                "docx2typed", "audit", "apply", str(workdir_path),
                "--scan", str(scan_path), "--policy", str(policy_path),
                "-o", str(output_path), "--workdir-out", str(Path(args.workdir_out).resolve()),
            ],
            status="ok",
            started_at=started,
            finished_at=_now(),
            inputs={
                "workdir": str(workdir_path),
                "scan_artifact": str(scan_path),
                "scan_artifact_sha256": scan["scan_artifact_sha256"],
                "policy": str(policy_path),
                "policy_sha256": policy_sha256(policy),
                **_snapshot_refs(scan),
            },
            outputs=outputs,
            policy_sha256=policy_sha256(policy),
            scan_artifact_sha256=scan["scan_artifact_sha256"],
            diagnostics=None,
        )
        _safe_write_run_evidence(evidence, output_path)
        print(f"normalized-workdir: {new_workdir}")
        return 0
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        inputs: dict[str, Any] = {"workdir": str(workdir_path)}
        if scan is not None:
            inputs.update(
                {
                    "scan_artifact": str(scan_path),
                    "scan_artifact_sha256": scan["scan_artifact_sha256"],
                    **_snapshot_refs(scan),
                }
            )
        if policy is not None:
            inputs["policy"] = str(policy_path)
            inputs["policy_sha256"] = policy_sha256(policy)
        evidence = run_evidence(
            command=[
                "docx2typed", "audit", "apply", str(workdir_path),
                "--scan", str(scan_path), "--policy", str(policy_path),
                "-o", str(output_path), "--workdir-out", str(Path(args.workdir_out).resolve()),
            ],
            status="error",
            started_at=started,
            finished_at=_now(),
            inputs=inputs,
            outputs={},
            policy_sha256=policy_sha256(policy) if policy is not None else None,
            scan_artifact_sha256=scan["scan_artifact_sha256"] if scan is not None else None,
            diagnostics=str(exc),
        )
        _safe_write_run_evidence(evidence, output_path)
        print(f"ERROR: {exc}")
        return 1


def audit(argv: list[str] | None = None) -> int:
    """Explicit scan → policy → apply governance for vertical normalization."""
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="docx2typed audit",
        description=(
            "Explicit scan → policy → apply governance for vertical normalization. "
            "`audit scan` is read-only and produces a scan artifact of candidates "
            "bound to a workdir snapshot; a complete, approved policy is required "
            "before `audit apply` runs the existing normalization transformation. "
            "There is no auto-apply."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    scan_parser = sub.add_parser(
        "scan",
        help="write a read-only scan artifact for a typed workdir",
        description=(
            "Validate the typed workdir and write the shared scan artifact "
            "(schema vertical-normalization-scan-1). Read-only: the workdir is "
            "never modified. The scan binds every candidate to a deterministic "
            "workdir snapshot and fingerprints it for later policy decisions."
        ),
    )
    scan_parser.add_argument("workdir", help="typed workdir to scan (read-only)")
    scan_parser.add_argument("-o", "--output", required=True, help="scan artifact JSON to write")
    apply_parser = sub.add_parser(
        "apply",
        help="apply a complete, approved normalization policy",
        description=(
            "Load and validate the scan artifact and policy, require a complete "
            "and approved policy bound to that exact scan and workdir snapshot, "
            "then run the existing normalization transformation. Refuses "
            "incomplete, stale, or unapproved policies before any transformation. "
            "The original workdir is left unchanged; a new normalized DOCX and "
            "workdir are emitted only after validation and independent "
            "verification succeed."
        ),
    )
    apply_parser.add_argument("workdir", help="typed workdir to normalize from (left unchanged)")
    apply_parser.add_argument("--scan", required=True, help="scan artifact JSON from `audit scan`")
    apply_parser.add_argument("--policy", required=True, help="complete, approved normalization policy JSON")
    apply_parser.add_argument("-o", "--output", required=True, help="normalized DOCX to write")
    apply_parser.add_argument("--workdir-out", required=True, help="new normalized workdir to create")
    parser.parse_args(argv)
    if argv and argv[0] == "scan":
        return _run_scan(argv[1:])
    return _run_apply(argv[1:])


if __name__ == "__main__":
    sys.exit(audit())
