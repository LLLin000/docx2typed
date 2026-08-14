"""Language-independent Protocol-major-1 descriptors and envelopes."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import sysconfig
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent

CONTRACT_RANGES = {
    name: {"major": 1, "min_minor": 0, "max_minor": 0}
    for name in ("cli", "mcp", "result", "evidence", "workdir")
}
FEATURES = ("hybrid-fidelity", "locked-structure", "typed-mode")
REQUIRED_FEATURES = FEATURES
# Finite operations that emit one docx2typed-result-1 envelope in --json mode.
# Commands without envelope support (view/normalize/audit) are intentionally
# NOT listed: their machine contract is not frozen by this protocol major.
PROTOCOL_COMMANDS = (
    "build",
    "decide",
    "edit",
    "extract",
    "inspect",
    "migrate",
    "validate",
    "verify",
)
PROTOCOL_TOOLS = ("engine_info", "workdir_open")
_WORKDIR_ASSETS = (
    "_template.docx",
    "edit.md",
    "edit.state.json",
    "format.json",
    "styles.json",
    "typed.md",
)


class ProtocolMismatch(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def semantic_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def schema_bundle() -> dict[str, Any]:
    path = Path(__file__).with_name("protocol_schema_bundle.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _capability_manifest_path() -> Path:
    source = Path(__file__).resolve().parent.parent / "capabilities" / "manifest.json"
    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "docx2typed"
        / "manifest.json"
    )
    for path in (source, installed):
        if path.is_file():
            return path
    raise FileNotFoundError("installed capability manifest is missing")


@lru_cache(maxsize=1)
def capability_manifest() -> dict[str, Any]:
    return json.loads(_capability_manifest_path().read_text(encoding="utf-8"))


def _package_version() -> str:
    try:
        return importlib.metadata.version("docx2typed")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0rc1"


@lru_cache(maxsize=1)
def engine_descriptor() -> dict[str, Any]:
    bundle = schema_bundle()
    manifest = capability_manifest()
    return {
        "schema": "docx2typed-engine-descriptor-1",
        "name": "docx2typed-python",
        "version": _package_version(),
        "build_commit": os.environ.get("DOCX2TYPED_BUILD_COMMIT", "unknown"),
        "target": f"{sysconfig.get_platform()}-{platform.python_implementation().lower()}",
        "contracts": CONTRACT_RANGES,
        "schema_bundle": {
            "schema": bundle["schema"],
            "sha256": semantic_sha256(bundle),
        },
        "capability_manifest": {
            "schema": manifest["schema"],
            "sha256": semantic_sha256(manifest),
        },
        "commands": {
            "finite": list(PROTOCOL_COMMANDS),
            "launchers": ["mcp"],
        },
        "tools": list(PROTOCOL_TOOLS),
        "features": list(FEATURES),
        "required_features": list(REQUIRED_FEATURES),
    }


def diagnostic(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    spec = schema_bundle()["diagnostics"][code]
    result: dict[str, Any] = {
        "schema": "docx2typed-diagnostic-1",
        "code": code,
        "severity": spec["severity"],
        "category": spec["category"],
        "retriable": spec["retriable"],
        "message": message,
    }
    if details is not None:
        result["details"] = details
    if next_actions:
        result["next_actions"] = next_actions
    return result


def domain_code_from_message(message: str) -> str:
    """Stable diagnostic code from a failure message prefix
    (``kebab-code: detail``); falls back to ``workdir-invalid`` when the
    prefix is not a registered code (issue #53: the CLI and MCP seams must
    surface the identical registered code for the same domain failure)."""
    candidate = message.split(":", 1)[0].strip().lower().replace(" ", "-")
    if candidate in schema_bundle()["diagnostics"]:
        return candidate
    return "workdir-invalid"


def domain_diagnostic(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Diagnostic for a tool/domain failure code.

    Uses the public registry when the code is registered; unregistered
    tool-documented codes (e.g. ``text-not-found``) still produce the frozen
    ``docx2typed-diagnostic-1`` shape with a stable domain default, so a
    failure envelope never depends on registry membership.
    """
    spec = schema_bundle()["diagnostics"].get(code)
    if spec is None:
        spec = {"severity": "error", "category": "domain", "retriable": False}
    result: dict[str, Any] = {
        "schema": "docx2typed-diagnostic-1",
        "code": code,
        "severity": spec["severity"],
        "category": spec["category"],
        "retriable": spec["retriable"],
        "message": message,
    }
    if details is not None:
        result["details"] = details
    if next_actions:
        result["next_actions"] = next_actions
    return result


def result_envelope(
    operation: str,
    outcome: str,
    *,
    data: dict[str, Any] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "docx2typed-result-1",
        "operation": operation,
        "outcome": outcome,
        "data": data or {},
        "diagnostics": diagnostics or [],
        "evidence": evidence or [],
        "engine": engine_descriptor(),
    }


def typed_path(path: str | Path) -> dict[str, str]:
    return {"kind": "absolute", "value": str(Path(path).resolve())}


def negotiate(
    contract_ranges: dict[str, dict[str, int]] | None = None,
    supported_features: list[str] | None = None,
    required_features: list[str] | None = None,
) -> None:
    for name, client in (contract_ranges or {}).items():
        engine = CONTRACT_RANGES.get(name)
        compatible = (
            engine is not None
            and client.get("major") == engine["major"]
            and client.get("min_minor", 0) <= engine["max_minor"]
            and client.get("max_minor", client.get("min_minor", 0)) >= engine["min_minor"]
        )
        if not compatible:
            raise ProtocolMismatch(
                "contract-incompatible",
                f"no compatible {name} contract version",
                {"contract": name, "engine_range": engine, "client_range": client},
            )
    engine_required: set[str] = set(REQUIRED_FEATURES)
    engine_features: set[str] = set(FEATURES)
    client_supported: set[str] = (
        set(supported_features) if supported_features is not None else engine_required
    )
    missing_engine_requirements = sorted(engine_required - client_supported)
    missing_client_requirements = sorted(set(required_features or ()) - engine_features)
    missing = sorted(set(missing_engine_requirements + missing_client_requirements))
    if missing:
        raise ProtocolMismatch(
            "required-feature-unsupported",
            "required features are unsupported",
            {"missing_features": missing},
        )


def derived_workdir_manifest(path: str | Path) -> dict[str, Any]:
    root = Path(path)
    assets = []
    for name in _WORKDIR_ASSETS:
        asset = root / name
        if asset.is_file():
            assets.append(
                {
                    "path": name,
                    "bytes": asset.stat().st_size,
                    "sha256": file_sha256(asset),
                }
            )
    # ponytail: derived bridge only; issue #49 replaces this with a persisted manifest.
    return {"schema": "docx2typed-derived-workdir-manifest-1", "assets": assets}


def mcp_result(envelope: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    summary = f"{envelope['operation']}: {envelope['outcome']}"
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent=envelope,
        isError=is_error,
    )


# --------------------------------------------------------------------------
# Operation IDs, run evidence, and the idempotency ledger
# --------------------------------------------------------------------------

EVIDENCE_SCHEMA = "docx2typed-run-evidence-1"
LEDGER_SCHEMA = "docx2typed-operation-ledger-1"


def new_operation_id() -> str:
    """Caller-visible operation identity; CLI auto-generates one, MCP requires
    the caller to supply it. UUID hex: unique, never derived from inputs."""
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_operation_input(operation: str, args: dict[str, Any]) -> str:
    """Stable identity of one operation attempt: operation name plus the
    canonical argument payload (including content hashes of input files).
    Time, host, and run identity never participate, so an identical retry
    hashes identically and a changed input hashes differently."""
    return semantic_sha256({"operation": operation, "args": args})


def base_evidence_payload() -> dict[str, Any]:
    """Engine and contract identity shared by every run-evidence payload."""
    return {
        "engine": {"name": "docx2typed-python", "version": _package_version()},
        "contracts": {
            "result": dict(CONTRACT_RANGES["result"]),
            "evidence": dict(CONTRACT_RANGES["evidence"]),
        },
    }


def run_evidence(
    operation: str,
    outcome: str,
    *,
    kind: str,
    operation_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """One canonical immutable run-evidence record.

    ``payload`` is the semantic part (inputs/outputs hashes, engine and
    contract identity, checks, decision ids) and must never carry document
    bodies, comment text, raw XML, secrets, or unnecessary absolute paths.
    ``payload_sha256`` covers exactly the canonical semantic payload;
    provenance (time, run identity) is excluded from semantic equivalence.
    """
    canonical_payload = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return {
        "schema": EVIDENCE_SCHEMA,
        "operation": operation,
        "outcome": outcome,
        "kind": kind,
        "operation_id": operation_id,
        "payload": canonical_payload,
        "payload_sha256": semantic_sha256(canonical_payload),
        "provenance": {
            "run_id": new_operation_id(),
            "started_at": _now_iso(),
            "finished_at": _now_iso(),
        },
    }


def publish_run_evidence(path: str | Path, evidence: dict[str, Any]) -> None:
    """Persist one run-evidence record atomically beside the operation's
    artifact. Raises OSError when the evidence cannot be published — callers
    must then report the mutation as failed, never as success."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temp_path, target)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def operation_ledger_path(anchor: str | Path, *, directory: bool = False) -> Path:
    """Persisted idempotency ledger location beside the operation's artifact.

    A directory anchor (workdir, new workdir) yields ``<dir>/operation-ledger.
    json``; a file anchor (built/verified DOCX) yields ``<file>.operation-ledger.
    json``. Temporary storage seam: issue #50 replaces this with the durable
    recovery ledger; until then the record is atomic-JSON best-effort.
    """
    path = Path(anchor)
    if directory or (path.exists() and path.is_dir()):
        return path / "operation-ledger.json"
    return Path(str(path) + ".operation-ledger.json")


def _result_envelope_ok(envelope: Any) -> bool:
    """Full ``docx2typed-result-1`` envelope shape: every required field
    present with the correct type. Completed ledger rows and prepared pending
    envelopes must both pass so replay can never emit a malformed Result."""
    if not isinstance(envelope, dict):
        return False
    if envelope.get("schema") != "docx2typed-result-1":
        return False
    if not isinstance(envelope.get("operation"), str) or not envelope["operation"]:
        return False
    if envelope.get("outcome") not in ("success", "failure", "partial"):
        return False
    if not isinstance(envelope.get("data"), dict):
        return False
    diagnostics = envelope.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(
        isinstance(item, dict) for item in diagnostics
    ):
        return False
    evidence = envelope.get("evidence")
    if not isinstance(evidence, list) or not all(
        isinstance(item, dict) for item in evidence
    ):
        return False
    if not isinstance(envelope.get("engine"), dict):
        return False
    return True


def _ledger_record_ok(record: Any) -> bool:
    """Shape validation of one persisted ledger record.

    Pending semantics are explicit: a pre-publish row MUST carry
    ``pending: true`` (and may carry the prepared complete envelope, which
    must itself be a full valid Result); a completed row MUST have ``pending``
    absent/false and carry a full ``docx2typed-result-1`` envelope. Any other
    shape is corrupt and is reported (not silently dropped) so callers fail
    closed instead of replaying or rerunning a possibly-completed effect."""
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("input_sha256"), str) or not record["input_sha256"]:
        return False
    if record.get("pending") not in (None, True, False):
        return False
    envelope = record.get("envelope")
    if envelope is None:
        return record.get("pending") is True  # pre-publish pending record
    return _result_envelope_ok(envelope)


def _read_ledger_file(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """(valid records, corrupt rows keyed by operation_id).

    Corrupt rows are never silently dropped: writers preserve them verbatim
    and readers report them, so a retry of the same operation_id fails closed
    (``operation-ledger-invalid``) instead of treating a possibly-completed
    effect as no record and mutating again."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return {}, {}
    valid: dict[str, dict[str, Any]] = {}
    corrupt: dict[str, Any] = {}
    for operation_id, record in records.items():
        if _ledger_record_ok(record):
            valid[operation_id] = record
        else:
            corrupt[operation_id] = record
    return valid, corrupt


def _write_ledger_file(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(
            json.dumps(
                {"schema": LEDGER_SCHEMA, "records": records},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class OperationLedger:
    """Idempotency ledger: in-process mirror plus per-artifact persisted JSON.

    A record answers replay (identical canonical input -> original Result) or
    rejects reuse (changed canonical input -> ``operation-id-reused``). The
    persisted copy makes CLI retries observable across processes; persistence
    is best-effort until the recovery ticket (#50) defines durability.
    """

    @staticmethod
    def _scope(anchor: str | Path, *, directory: bool = False) -> str:
        """Per-store namespace for the in-memory mirror: the resolved ledger
        file location. Records are keyed by ``(scope, operation_id)`` so two
        workdirs sharing one process and one operation_id never consult each
        other's in-memory replay (issue #50 finding 3); replay within one
        store stays byte-exact because it always hits the same scope."""
        return str(operation_ledger_path(anchor, directory=directory).resolve())

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def lookup(
        self, operation_id: str, anchor: str | Path | None = None, *, directory: bool = False
    ) -> dict[str, Any] | None:
        """In-process mirror lookup, namespaced by the ledger file location.

        Without an anchor there is no namespaced record to return: the
        unscoped global mirror is gone, so a caller must scope by its own
        ledger anchor (workdir/generation) and never sees another workdir's
        records (issue #50 finding 3)."""
        if anchor is None:
            return None
        with self._lock:
            return self._records.get(
                (self._scope(anchor, directory=directory), operation_id)
            )

    def lookup_persisted(
        self,
        operation_id: str,
        anchor: str | Path,
        *,
        directory: bool = False,
    ) -> dict[str, Any] | None:
        """Namespaced mirror-first lookup, then the persisted ledger at
        ``anchor``. The mirror only answers for the SAME ledger file, so an
        in-memory record written by another workdir is never replayed here."""
        key = self._scope(anchor, directory=directory)
        with self._lock:
            record = self._records.get((key, operation_id))
        if record is not None:
            return record
        record = _read_ledger_file(operation_ledger_path(anchor, directory=directory))[0].get(
            operation_id
        )
        if record is not None:
            with self._lock:
                self._records.setdefault((key, operation_id), record)
        return record

    def corrupt_persisted(
        self,
        operation_id: str,
        anchor: str | Path,
        *,
        directory: bool = False,
    ) -> Path | None:
        """Return the ledger file holding a structurally corrupt row for
        ``operation_id`` (None when clean): the caller must fail closed
        (``operation-ledger-invalid``) naming that exact file instead of
        rerunning a possibly-completed effect."""
        path = operation_ledger_path(anchor, directory=directory)
        _, corrupt = _read_ledger_file(path)
        return path if operation_id in corrupt else None

    def lookup_file(
        self, operation_id: str, ledger_file: str | Path
    ) -> dict[str, Any] | None:
        """Idempotency lookup against one explicit ledger file.

        Used when the ledger must live at a caller-chosen path (migrate keeps
        its replay ledger beside the target so a failed migration never
        creates the target directory). The mirror is namespaced by the file,
        so distinct targets sharing one operation_id stay isolated."""
        key = str(Path(ledger_file).resolve())
        with self._lock:
            record = self._records.get((key, operation_id))
        if record is not None:
            return record
        record = _read_ledger_file(Path(ledger_file))[0].get(operation_id)
        if record is not None:
            with self._lock:
                self._records.setdefault((key, operation_id), record)
        return record

    def corrupt_file(self, operation_id: str, ledger_file: str | Path) -> bool:
        """True when the explicit ledger file holds a structurally corrupt row
        for ``operation_id`` (see ``corrupt_persisted``)."""
        _, corrupt = _read_ledger_file(Path(ledger_file))
        return operation_id in corrupt

    def record_file(
        self,
        operation_id: str,
        input_sha256: str,
        envelope: dict[str, Any] | None,
        ledger_file: str | Path,
        *,
        pending: bool = False,
    ) -> None:
        """Persist one record into an explicit ledger file (best-effort).

        ``envelope`` is None for a pre-publish pending record: the operation
        started but no final Result exists yet. A retry then either reruns the
        operation or reconstructs the success from the published target.
        ``pending=True`` marks a record whose envelope is the precomputed
        success envelope prepared before the atomic publish: a retry replays
        it only after validating the published target against the source, and
        the completed upgrade reuses the same envelope byte-for-byte."""
        record = {"input_sha256": input_sha256, "envelope": envelope}
        if pending:
            record["pending"] = True
        key = str(Path(ledger_file).resolve())
        with self._lock:
            self._records[(key, operation_id)] = record
        try:
            path = Path(ledger_file)
            records, corrupt = _read_ledger_file(path)
            records.update(corrupt)  # preserve corrupt rows: they fail closed on their own op_id
            records[operation_id] = record
            _write_ledger_file(path, records)
        except OSError:
            pass  # ledger persistence is best-effort; idempotency degrades to in-process only

    def forget_file(
        self,
        operation_id: str,
        ledger_file: str | Path,
    ) -> None:
        """Drop one record from an explicit ledger file (best-effort).

        Used to remove a pre-publish pending record after a failed run so a
        later retry with the same operation_id starts fresh instead of being
        rejected as reused."""
        key = str(Path(ledger_file).resolve())
        with self._lock:
            self._records.pop((key, operation_id), None)
        try:
            path = Path(ledger_file)
            records, corrupt = _read_ledger_file(path)
            records.update(corrupt)
            records.pop(operation_id, None)
            _write_ledger_file(path, records)
        except OSError:
            pass  # ledger persistence is best-effort; idempotency degrades to in-process only

    def record(
        self,
        operation_id: str,
        input_sha256: str,
        envelope: dict[str, Any],
        anchor: str | Path,
        *,
        directory: bool = False,
        pending: bool = False,
    ) -> None:
        record = {"input_sha256": input_sha256, "envelope": envelope}
        if pending:
            record["pending"] = True
        key = self._scope(anchor, directory=directory)
        with self._lock:
            self._records[(key, operation_id)] = record
        try:
            path = operation_ledger_path(anchor, directory=directory)
            records, corrupt = _read_ledger_file(path)
            records.update(corrupt)
            records[operation_id] = record
            _write_ledger_file(path, records)
        except OSError:
            pass  # ledger persistence is best-effort; idempotency degrades to in-process only


operation_ledger = OperationLedger()
