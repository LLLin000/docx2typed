"""Language-independent Protocol-major-1 descriptors and envelopes."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import sysconfig
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
PROTOCOL_COMMANDS = ("validate",)
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
