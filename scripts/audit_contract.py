"""Frozen audit contract: explicit scan → policy → apply governance.

This module is the single source of truth for the governance schemas shared by
the audit CLI and the normalization application primitive:

- scan schema    ``vertical-normalization-scan-1``
- policy schema  ``vertical-normalization-policy-2``
- audit schema   ``vertical-normalization-audit-2``

Everything here is filesystem-agnostic: functions take/return plain dicts and
raise :class:`~scripts.typed_docx.ValidationError` on governance violations.
Classification is never a decision; applying a policy never happens implicitly.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

try:
    from .typed_docx import ValidationError
except ImportError:  # direct script execution has no package context.
    from typed_docx import ValidationError

SCAN_SCHEMA = "vertical-normalization-scan-1"
POLICY_SCHEMA = "vertical-normalization-policy-2"
AUDIT_SCHEMA = "vertical-normalization-audit-2"
RUN_EVIDENCE_SCHEMA = "vertical-normalization-run-evidence-1"
SCANNER_CONTRACT_VERSION = 1

POLICY_STATUSES = ("draft", "reviewed", "approved", "superseded", "applied", "rejected")
APPROVAL_REQUIREMENTS = ("human", "self")
DECISIONS = ("convert", "preserve")
RISKY_CLASSIFICATIONS = ("ambiguous", "manual", "unsupported")
CLASSIFICATIONS = ("approved",) + RISKY_CLASSIFICATIONS

def _risk_reasons(candidate: dict[str, Any]) -> tuple[str, ...]:
    reasons = [candidate["classification"]] if candidate.get("classification") in RISKY_CLASSIFICATIONS else []
    if candidate.get("reversible") is False:
        reasons.append("non_reversible")
    features = candidate.get("word_style_features") or {}
    current_vertical = candidate.get("word_vert_align")
    if (current_vertical and current_vertical != candidate.get("vertical")) or features.get("position"):
        reasons.append("conflicting_style")
    return tuple(dict.fromkeys(reasons))

SNAPSHOT_FIELDS = (
    "project_id",
    "baseline_sha256",
    "draft_snapshot_sha256",
    "model_sha256",
    "catalog_sha256",
    "scanner_contract_version",
)
CANDIDATE_FIELDS = (
    "candidate_id",
    "occurrence_id",
    "paragraph_id",
    "node_path",
    "visible_offset",
    "codepoint",
    "source",
    "style_id",
    "classification",
    "proposed_target",
    "vertical",
    "reversible",
)

_MODEL_IDENTITY = "docx2typed-vertical-normalization-scanner-model-1"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_sha256(data: dict[str, Any], *exclude: str) -> str:
    """Deterministic canonical-JSON hash of a payload.

    Pass the payload's own self-reference hash key (e.g. ``scan_artifact_sha256``)
    in ``exclude`` so the hash is a pure function of the content.
    """
    base = {key: value for key, value in data.items() if key not in exclude}
    return sha256_hex(_canonical_json(base))


def _require_sha256(field: str, value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError(f"invalid {field}: expected a lowercase SHA-256 hex digest")


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------

def fallback_project_id(baseline_sha256: str) -> str:
    """Deterministic project identity derived from the workdir baseline.

    Used when the current workdir has no explicit project manifest; it is not a
    second editable truth source.
    """
    _require_sha256("baseline_sha256", baseline_sha256)
    return "project-" + sha256_hex(f"baseline:{baseline_sha256}".encode("utf-8"))[:16]


def default_model_sha256() -> str:
    """Identity of the bundled vertical-normalization scanner model."""
    return sha256_hex(_MODEL_IDENTITY.encode("utf-8"))


def create_snapshot(
    *,
    project_id: str | None = None,
    baseline_sha256: str,
    draft_snapshot_sha256: str,
    model_sha256: str | None = None,
    catalog_sha256: str,
    scanner_contract_version: int = SCANNER_CONTRACT_VERSION,
) -> dict[str, Any]:
    _require_sha256("baseline_sha256", baseline_sha256)
    _require_sha256("draft_snapshot_sha256", draft_snapshot_sha256)
    _require_sha256("catalog_sha256", catalog_sha256)
    if model_sha256 is None:
        model_sha256 = default_model_sha256()
    _require_sha256("model_sha256", model_sha256)
    if scanner_contract_version != SCANNER_CONTRACT_VERSION:
        raise ValidationError(f"unsupported scanner contract version: {scanner_contract_version}")
    return {
        "project_id": project_id or fallback_project_id(baseline_sha256),
        "baseline_sha256": baseline_sha256,
        "draft_snapshot_sha256": draft_snapshot_sha256,
        "model_sha256": model_sha256,
        "catalog_sha256": catalog_sha256,
        "scanner_contract_version": scanner_contract_version,
    }


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValidationError("scan snapshot must be an object")
    for field in SNAPSHOT_FIELDS:
        if field not in snapshot:
            raise ValidationError(f"scan snapshot is missing field: {field}")
    if not isinstance(snapshot["project_id"], str) or not snapshot["project_id"]:
        raise ValidationError("invalid project_id")
    for field in ("baseline_sha256", "draft_snapshot_sha256", "model_sha256", "catalog_sha256"):
        _require_sha256(field, snapshot[field])
    if snapshot["scanner_contract_version"] != SCANNER_CONTRACT_VERSION:
        raise ValidationError(f"unsupported scanner contract version: {snapshot['scanner_contract_version']}")
    return snapshot


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return payload_sha256(validate_snapshot(snapshot))


# --------------------------------------------------------------------------
# Scan artifact
# --------------------------------------------------------------------------

def fingerprint_candidate(candidate: dict[str, Any], snapshot: dict[str, Any]) -> str:
    """Deterministic candidate fingerprint bound to the scan snapshot.

    Any change to the snapshot (project, baseline, draft, model, catalog,
    scanner version) or to the candidate identity invalidates the fingerprint.
    """
    snapshot = validate_snapshot(snapshot)
    payload: dict[str, Any] = {"snapshot_sha256": snapshot_sha256(snapshot)}
    for field in SNAPSHOT_FIELDS:
        payload[field] = snapshot[field]
    for field in CANDIDATE_FIELDS:
        if field not in candidate:
            raise ValidationError(f"scan candidate is missing field: {field}")
        payload[field] = candidate[field]
    return sha256_hex(_canonical_json(payload))


def create_scan_artifact(*, snapshot: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot = validate_snapshot(snapshot)
    if not isinstance(candidates, list):
        raise ValidationError("scan candidates must be a list")
    bound: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, dict):
            raise ValidationError("scan candidate must be an object")
        candidate = dict(raw)
        for field in CANDIDATE_FIELDS:
            if field not in candidate:
                raise ValidationError(f"scan candidate is missing field: {field}")
        if not isinstance(candidate["visible_offset"], int) or candidate["visible_offset"] < 0:
            raise ValidationError("visible_offset must be a non-negative integer")
        if not isinstance(candidate["source"], str) or len(candidate["source"]) != 1:
            raise ValidationError("candidate source must be a single code point")
        if candidate["codepoint"] != f"U+{ord(candidate['source']):04X}":
            raise ValidationError(f"codepoint does not match source: {candidate['occurrence_id']}")
        if candidate["classification"] not in CLASSIFICATIONS:
            raise ValidationError(f"invalid classification: {candidate['classification']}")
        if candidate["occurrence_id"] in seen:
            raise ValidationError(f"duplicate occurrence: {candidate['occurrence_id']}")
        seen.add(candidate["occurrence_id"])
        expected = fingerprint_candidate(candidate, snapshot)
        stored = candidate.get("candidate_fingerprint")
        if stored is not None and stored != expected:
            raise ValidationError(f"candidate fingerprint mismatch: {candidate['occurrence_id']}")
        candidate["candidate_fingerprint"] = expected
        bound.append(candidate)
    scan: dict[str, Any] = {
        "schema": SCAN_SCHEMA,
        "scanner_contract_version": snapshot["scanner_contract_version"],
        "snapshot": snapshot,
        "candidates": bound,
    }
    scan["scan_artifact_sha256"] = payload_sha256(scan, "scan_artifact_sha256")
    return scan


def validate_scan_artifact(scan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(scan, dict):
        raise ValidationError("scan artifact must be an object")
    if scan.get("schema") != SCAN_SCHEMA:
        raise ValidationError(f"incompatible scan artifact: expected {SCAN_SCHEMA}")
    if scan.get("scanner_contract_version") != SCANNER_CONTRACT_VERSION:
        raise ValidationError("incompatible scanner contract version")
    validate_snapshot(scan.get("snapshot"))
    candidates = scan.get("candidates")
    if not isinstance(candidates, list):
        raise ValidationError("scan artifact candidates must be a list")
    seen_occurrences: set[str] = set()
    seen_candidates: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValidationError("scan candidate must be an object")
        for field in CANDIDATE_FIELDS:
            if field not in candidate:
                raise ValidationError(f"scan candidate is missing field: {field}")
        if not isinstance(candidate["visible_offset"], int) or candidate["visible_offset"] < 0:
            raise ValidationError("visible_offset must be a non-negative integer")
        if not isinstance(candidate["source"], str) or len(candidate["source"]) != 1:
            raise ValidationError("candidate source must be a single code point")
        if candidate["codepoint"] != f"U+{ord(candidate['source']):04X}":
            raise ValidationError(f"codepoint does not match source: {candidate['occurrence_id']}")
        if candidate["classification"] not in CLASSIFICATIONS:
            raise ValidationError(f"invalid classification: {candidate['classification']}")
        occurrence_id = candidate["occurrence_id"]
        candidate_id = candidate["candidate_id"]
        if occurrence_id in seen_occurrences:
            raise ValidationError(f"duplicate occurrence: {occurrence_id}")
        if candidate_id in seen_candidates:
            raise ValidationError(f"duplicate candidate: {candidate_id}")
        seen_occurrences.add(occurrence_id)
        seen_candidates.add(candidate_id)
        expected = fingerprint_candidate(candidate, scan["snapshot"])
        if candidate.get("candidate_fingerprint") != expected:
            raise ValidationError(f"candidate fingerprint mismatch: {occurrence_id}")
    stored = scan.get("scan_artifact_sha256")
    if stored != payload_sha256(scan, "scan_artifact_sha256"):
        raise ValidationError("scan artifact hash mismatch")
    return scan


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

def create_policy(
    *,
    scan: dict[str, Any],
    status: str = "draft",
    approval_requirement: str = "human",
    decisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scan = validate_scan_artifact(scan)
    if status not in POLICY_STATUSES:
        raise ValidationError(f"invalid policy status: {status!r}")
    if approval_requirement not in APPROVAL_REQUIREMENTS:
        raise ValidationError(f"invalid approval requirement: {approval_requirement!r}")
    if decisions is None:
        decisions = {}
    if not isinstance(decisions, dict):
        raise ValidationError("policy decisions must be an object")
    snapshot = scan["snapshot"]
    policy: dict[str, Any] = {
        "schema": POLICY_SCHEMA,
        "status": status,
        "approval_requirement": approval_requirement,
        "audit_schema": AUDIT_SCHEMA,
        "scanner_contract_version": snapshot["scanner_contract_version"],
        "project_id": snapshot["project_id"],
        "baseline_sha256": snapshot["baseline_sha256"],
        "draft_snapshot_sha256": snapshot["draft_snapshot_sha256"],
        "model_sha256": snapshot["model_sha256"],
        "catalog_sha256": snapshot["catalog_sha256"],
        "scan_artifact_sha256": scan["scan_artifact_sha256"],
        "decisions": decisions,
    }
    return policy


def policy_sha256(policy: dict[str, Any]) -> str:
    """Canonical hash of a policy; a policy is bound by its exact content."""
    return payload_sha256(policy)


def validate_policy(
    policy: dict[str, Any],
    *,
    scan: dict[str, Any],
    catalog_sha256: str | None = None,
    require_approved: bool = False,
) -> dict[str, Any]:
    """Validate policy structure, snapshot bindings, and completeness.

    Rejects stale project/draft/model/catalog/scanner bindings, a mismatched
    scan artifact, unknown or missing occurrences, pending decisions, missing
    actors, missing required rationale, non-approved conversions, and (with
    ``require_approved``) any status other than ``approved`` with a valid
    approval object.
    """
    scan = validate_scan_artifact(scan)
    if not isinstance(policy, dict):
        raise ValidationError("normalization policy must be an object")
    if policy.get("schema") != POLICY_SCHEMA:
        raise ValidationError(f"incompatible normalization policy: expected {POLICY_SCHEMA}")
    status = policy.get("status")
    if status not in POLICY_STATUSES:
        raise ValidationError(f"invalid policy status: {status!r}")
    requirement = policy.get("approval_requirement")
    if requirement not in APPROVAL_REQUIREMENTS:
        raise ValidationError(f"invalid approval requirement: {requirement!r}")
    if policy.get("audit_schema") != AUDIT_SCHEMA:
        raise ValidationError("policy targets an incompatible audit schema")
    if policy.get("scanner_contract_version") != SCANNER_CONTRACT_VERSION:
        raise ValidationError("policy uses an incompatible scanner contract version")
    snapshot = scan["snapshot"]
    for field in SNAPSHOT_FIELDS:
        if policy.get(field) != snapshot[field]:
            raise ValidationError(f"policy is stale: {field} mismatch with scan snapshot")
    if policy.get("scan_artifact_sha256") != scan["scan_artifact_sha256"]:
        raise ValidationError("policy references a different scan artifact")
    if catalog_sha256 is not None and catalog_sha256 != snapshot["catalog_sha256"]:
        raise ValidationError("policy catalog does not match the current catalog")
    decisions = policy.get("decisions")
    if not isinstance(decisions, dict):
        raise ValidationError("policy decisions must be an object")
    candidate_by_id = {candidate["occurrence_id"]: candidate for candidate in scan["candidates"]}
    unknown = set(decisions) - set(candidate_by_id)
    if unknown:
        raise ValidationError("policy references unknown occurrences: " + ", ".join(sorted(unknown)))
    for occurrence_id, candidate in candidate_by_id.items():
        decision = decisions.get(occurrence_id)
        if not isinstance(decision, dict):
            raise ValidationError(f"pending decision: {occurrence_id}")
        if decision.get("decision") not in DECISIONS:
            raise ValidationError(f"invalid or pending decision: {occurrence_id}")
        actor = decision.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            raise ValidationError(f"missing decision actor: {occurrence_id}")
        if decision.get("candidate_fingerprint") != candidate["candidate_fingerprint"]:
            raise ValidationError(f"decision fingerprint mismatch: {occurrence_id}")
        rationale = decision.get("rationale")
        if rationale is not None and not isinstance(rationale, str):
            raise ValidationError(f"invalid rationale: {occurrence_id}")
        risk_reasons = _risk_reasons(candidate)
        if risk_reasons and not rationale:
            raise ValidationError(
                f"risky classification requires rationale ({', '.join(risk_reasons)}): {occurrence_id}"
            )
        if decision["decision"] == "convert":
            if candidate["classification"] != "approved":
                raise ValidationError(f"non-approved classification cannot be converted: {occurrence_id}")
            if not candidate["proposed_target"]:
                raise ValidationError(f"candidate has no proposed target: {occurrence_id}")
    if require_approved:
        _require_approval(policy, requirement)
    return policy


def _require_approval(policy: dict[str, Any], requirement: str) -> None:
    if policy.get("status") != "approved":
        raise ValidationError(f"policy must be approved before apply (status: {policy.get('status')!r})")
    approval = policy.get("approval")
    if not isinstance(approval, dict):
        raise ValidationError("approved policy requires an explicit approval object")
    if approval.get("approved") is not True:
        raise ValidationError("approval object must record approved=true")
    if approval.get("requirement") != requirement:
        raise ValidationError("approval does not satisfy the policy approval requirement")
    if not isinstance(approval.get("approved_by"), str) or not approval["approved_by"].strip():
        raise ValidationError("approval object is missing approved_by")
    if not isinstance(approval.get("approval_time"), str) or not approval["approval_time"]:
        raise ValidationError("approval object is missing approval_time")


def require_complete(policy: dict[str, Any], scan: dict[str, Any]) -> dict[str, Any]:
    """Completeness only: every scan candidate decided, no pending items."""
    return validate_policy(policy, scan=scan)


def approve_policy(
    policy: dict[str, Any],
    *,
    scan: dict[str, Any],
    approved_by: str,
    approval_time: str | None = None,
) -> dict[str, Any]:
    """Deliberate approval step; never auto-approves.

    ``self`` approval is an explicit approval object like ``human`` — the
    policy's ``approval_requirement`` is satisfied by this same object, never
    inferred from who wrote the decisions.
    """
    validate_policy(policy, scan=scan)
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValidationError("approved_by must identify the approving actor")
    if approval_time is None:
        approval_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    approved = dict(policy)
    approved["status"] = "approved"
    approved["approval"] = {
        "approved": True,
        "requirement": policy["approval_requirement"],
        "approved_by": approved_by,
        "approval_time": approval_time,
    }
    return approved


def require_approved(
    policy: dict[str, Any],
    scan: dict[str, Any],
    catalog_sha256: str | None = None,
) -> dict[str, Any]:
    """Full apply gate: valid, complete, and explicitly approved."""
    return validate_policy(policy, scan=scan, catalog_sha256=catalog_sha256, require_approved=True)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

def audit_records(
    policy: dict[str, Any],
    scan: dict[str, Any],
    changes: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One occurrence-level change record per scan candidate, in scan order."""
    validate_policy(policy, scan=scan)
    scan_hash = scan["scan_artifact_sha256"]
    policy_hash = policy_sha256(policy)
    by_id = {candidate["occurrence_id"]: candidate for candidate in scan["candidates"]}
    changes = changes or {}
    records: list[dict[str, Any]] = []
    for candidate in scan["candidates"]:
        decision = policy["decisions"][candidate["occurrence_id"]]
        convert = decision["decision"] == "convert"
        change = changes.get(candidate["occurrence_id"], {})
        record: dict[str, Any] = {
            "occurrence_id": candidate["occurrence_id"],
            "candidate_id": candidate["candidate_id"],
            "paragraph_id": candidate["paragraph_id"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "old_text": candidate["source"],
            "new_text": candidate["proposed_target"] if convert else candidate["source"],
            "old_style_id": candidate["style_id"],
            "new_style_id": change.get("new_style_id", candidate["style_id"]),
            "style_delta": (
                {"vertical": candidate["vertical"], "reversible": candidate["reversible"]} if convert else None
            ),
            "classification": candidate["classification"],
            "decision": decision["decision"],
            "actor": decision["actor"],
            "context": candidate["context"],
            "scan_artifact_sha256": scan_hash,
            "policy_sha256": policy_hash,
        }
        if "rationale" in decision and decision["rationale"] is not None:
            record["rationale"] = decision["rationale"]
        records.append(record)
    return records


def create_audit(
    *,
    policy: dict[str, Any],
    scan: dict[str, Any],
    changes: dict[str, Any] | None = None,
    source_workdir: str | None = None,
    catalog_version: str | None = None,
    status: str = "applied",
) -> dict[str, Any]:
    """Audit artifact for an applied policy, with full snapshot binding."""
    require_approved(policy, scan=scan)
    snapshot = scan["snapshot"]
    audit: dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "status": status,
        "project_id": snapshot["project_id"],
        "baseline_sha256": snapshot["baseline_sha256"],
        "draft_snapshot_sha256": snapshot["draft_snapshot_sha256"],
        "model_sha256": snapshot["model_sha256"],
        "catalog_sha256": snapshot["catalog_sha256"],
        "scanner_contract_version": snapshot["scanner_contract_version"],
        "scan_artifact_sha256": scan["scan_artifact_sha256"],
        "policy_sha256": policy_sha256(policy),
        "catalog_version": catalog_version,
        "source_workdir": source_workdir,
        "occurrences": audit_records(policy, scan, changes),
    }
    return audit


# --------------------------------------------------------------------------
# Run evidence
# --------------------------------------------------------------------------

def run_evidence(
    *,
    command: str | list[str],
    status: str,
    started_at: str,
    finished_at: str,
    inputs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
    policy_sha256: str | None = None,
    scan_artifact_sha256: str | None = None,
    diagnostics: str | list[str] | None = None,
) -> dict[str, Any]:
    """Transitional run-evidence record stored beside scan/apply artifacts.

    Carries command, status, timestamps, input/output hashes, policy/scan
    references, and diagnostics. Does not claim full project-directory lineage.
    """
    if isinstance(command, list):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValidationError("run evidence requires a command")
    elif not isinstance(command, str) or not command:
        raise ValidationError("run evidence requires a command")
    if not isinstance(status, str) or not status:
        raise ValidationError("run evidence requires a status")
    for name in ("started_at", "finished_at"):
        value = {"started_at": started_at, "finished_at": finished_at}[name]
        if not isinstance(value, str) or not value:
            raise ValidationError(f"run evidence requires {name}")
    if isinstance(diagnostics, str):
        diagnostics_list = [diagnostics]
    elif isinstance(diagnostics, list) and all(isinstance(item, str) for item in diagnostics):
        diagnostics_list = diagnostics
    elif diagnostics is None:
        diagnostics_list = []
    else:
        raise ValidationError("run evidence diagnostics must be a string or list of strings")
    return {
        "schema": RUN_EVIDENCE_SCHEMA,
        "command": command,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "inputs": dict(inputs or {}),
        "outputs": dict(outputs or {}),
        "policy_sha256": policy_sha256,
        "scan_artifact_sha256": scan_artifact_sha256,
        "diagnostics": diagnostics_list,
    }
