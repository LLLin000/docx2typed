"""Issue #49: stable read-only ``inspect`` and lossless ``migrate`` for
schema-1 typed workdirs.

``inspect SOURCE`` classifies one schema-1 workdir: one stable readiness
classification, an asset table (kind / presence / hash / required /
read-only), stable reason codes, semantic state, baseline identities, and the
permitted next action. Inspect never writes to SOURCE: no locks, no state
files, no ledger, no evidence, and it snapshots every source file's bytes and
mtime so a caller can prove nothing changed.

``migrate SOURCE --out TARGET`` reads an immutable snapshot of SOURCE, stages
every asset byte-for-byte (mtimes preserved), verifies asset closure,
semantic state (including dirty / stale-clean / conflict / missing edit
state), typed validation, and observable behavior, writes the versioned
workdir manifest (``docx2typed-workdir-manifest-1``), publishes run evidence
beside the target, then atomically renames staging onto TARGET. The schema-1
source is never modified; any failure leaves no normal TARGET. Unknown files
are explicitly classified Opaque attachments (read-only, manifest-declared);
unknown required features fail closed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from .edit import classify_edit_state
    from .protocol import (
        FEATURES,
        REQUIRED_FEATURES,
        base_evidence_payload,
        derived_workdir_manifest,
        engine_descriptor,
        file_sha256,
        new_operation_id,
        publish_run_evidence,
        run_evidence,
        semantic_sha256,
        typed_path,
    )
    from .typed_core import TypedError
    from .typed_docx import build_workdir, validate_workdir
except ImportError:  # direct script execution has no package context.
    from edit import classify_edit_state
    from protocol import (
        FEATURES,
        REQUIRED_FEATURES,
        base_evidence_payload,
        derived_workdir_manifest,
        engine_descriptor,
        file_sha256,
        new_operation_id,
        publish_run_evidence,
        run_evidence,
        semantic_sha256,
        typed_path,
    )
    from typed_core import TypedError
    from typed_docx import build_workdir, validate_workdir

WORKDIR_MANIFEST_SCHEMA = "docx2typed-workdir-manifest-1"
MANIFEST_VERSION = 1
MANIFEST_FILE = "workdir.manifest.json"

# (name, role, read_only): the authoritative schema-1 asset set.
AUTHORITATIVE_ASSETS = (
    ("typed.md", "typed-ast", False),
    ("format.json", "format", False),
    ("styles.json", "styles", False),
    ("_template.docx", "template-baseline", True),
)

# (name, role): optional assets the engine understands. Presence is preserved
# exactly; absence is not an error.
OPTIONAL_ASSETS = (
    ("edit.md", "edit-projection"),
    ("edit.state.json", "edit-state"),
    ("edit.state.json.run.json", "edit-evidence"),
    ("revisions.json", "revisions"),
    ("revisions.md", "revisions-view"),
    ("regions.md", "regions"),
    ("decisions.json", "decisions"),
    ("run.evidence.json", "run-evidence"),
    ("operation-ledger.json", "operation-ledger"),
    ("workdir.manifest.json", "workdir-manifest"),
)

REVIEW_DIR = ".review"  # collaboration session/history/snapshots/inbox

_KNOWN_TOP_LEVEL = (
    {name for name, _, _ in AUTHORITATIVE_ASSETS}
    | {name for name, _ in OPTIONAL_ASSETS}
    | {REVIEW_DIR}
)

# Stable inspect reason codes. Blocking ones double as Protocol diagnostic
# codes; informational ones never flip readiness.
BLOCKING_REASONS = (
    "asset-closure",
    "schema-incompatible",
    "required-feature-unsupported",
    "source-drift",
    "symlink-detected",
)


class MigrateError(TypedError):
    """Migration failure carrying a stable Protocol diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _file_asset(
    rel: str,
    path: Path,
    *,
    kind: str,
    role: str,
    required: bool,
    read_only: bool,
) -> dict[str, Any]:
    if path.is_file():
        stat = path.stat()
        return {
            "path": rel,
            "kind": kind,
            "required": required,
            "read_only": read_only,
            "role": role,
            "presence": "present",
            "bytes": stat.st_size,
            "sha256": file_sha256(path),
            "mtime_ns": stat.st_mtime_ns,
        }
    return {
        "path": rel,
        "kind": kind,
        "required": required,
        "read_only": read_only,
        "role": role,
        "presence": "missing",
        "bytes": 0,
        "sha256": None,
        "mtime_ns": None,
    }


def _dir_digest(root: Path, files: Iterable[Path]) -> str:
    """Content digest of one directory asset, keyed by source-relative paths
    so same-basename descendants under different opaque subtrees never
    collide."""
    return semantic_sha256(
        {
            path.relative_to(root).as_posix(): file_sha256(path)
            for path in sorted(files)
        }
    )


def _is_link(path: Path) -> bool:
    """True for every link-like entry: POSIX symlinks plus Windows symlinks
    AND junctions (reparse points). ``Path.is_symlink`` misses junctions on
    some Windows builds, and os.walk would happily descend into one."""
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes:
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return stat.S_ISLNK(info.st_mode)


def _walk_files(top: Path) -> list[Path]:
    """Every regular file under ``top``, never following or dereferencing
    links (linked directories are not descended into and linked files are
    skipped). Inventory and migration therefore can never read through a
    link."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if not _is_link(Path(dirpath) / name)
        ]
        for name in filenames:
            candidate = Path(dirpath) / name
            if _is_link(candidate):
                continue
            if candidate.is_file():
                files.append(candidate)
    return files


def _symlink_paths(root: Path) -> list[str]:
    """Relative paths of every link anywhere under ``root`` (including
    ``.review`` and opaque subtrees). Never dereferences a link: linked
    directories are pruned from the walk so os.walk cannot descend into a
    junction or link (its own followlinks check misses Windows junctions)."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dir_path = Path(dirpath)
        linked = [name for name in dirnames if _is_link(dir_path / name)]
        found.extend(
            (dir_path / name).relative_to(root).as_posix() for name in linked
        )
        dirnames[:] = [name for name in dirnames if name not in linked]
        for name in filenames:
            candidate = dir_path / name
            if _is_link(candidate):
                found.append(candidate.relative_to(root).as_posix())
    return sorted(found)


def inventory_assets(root: Path) -> list[dict[str, Any]]:
    """Stable asset table for every file under ``root``.

    Authoritative assets are always listed (``presence: missing`` when
    absent); optional and opaque assets are listed when present. Everything
    unknown is classified Opaque (read-only). The ``.review`` collaboration
    tree is optional review state (lock files read-only). Read-only: this
    never creates or modifies anything under ``root``.
    """
    assets: list[dict[str, Any]] = []
    for name, role, read_only in AUTHORITATIVE_ASSETS:
        path = root / name
        if _is_link(path):
            continue  # rejected by inspect; never dereference the link
        assets.append(
            _file_asset(
                name,
                path,
                kind="authoritative",
                role=role,
                required=True,
                read_only=read_only,
            )
        )
    for name, role in OPTIONAL_ASSETS:
        path = root / name
        if _is_link(path):
            continue  # rejected by inspect; never dereference the link
        assets.append(
            _file_asset(
                name,
                path,
                kind="optional",
                role=role,
                required=False,
                read_only=name == "workdir.manifest.json",
            )
        )
    review = root / REVIEW_DIR
    if review.is_dir() and not _is_link(review):
        for path in sorted(_walk_files(review)):
            rel = path.relative_to(root).as_posix()
            lock = path.name.endswith(".lock")
            assets.append(
                _file_asset(
                    rel,
                    path,
                    kind="optional",
                    role="review-lock" if lock else "review-session",
                    required=False,
                    read_only=lock,
                )
            )
    for path in sorted(root.iterdir()):
        if path.name in _KNOWN_TOP_LEVEL:
            continue
        if _is_link(path):
            continue  # rejected by inspect; never dereference the link
        if path.is_dir():
            files = _walk_files(path)
            bytes_total = sum(p.stat().st_size for p in files)
            assets.append(
                {
                    "path": path.name + "/",
                    "kind": "opaque",
                    "required": False,
                    "read_only": True,
                    "role": "attachment",
                    "presence": "present",
                    "bytes": bytes_total,
                    "sha256": _dir_digest(root, files),
                    "mtime_ns": None,
                }
            )
        elif path.is_file():
            assets.append(
                _file_asset(
                    path.name,
                    path,
                    kind="opaque",
                    role="attachment",
                    required=False,
                    read_only=True,
                )
            )
    return assets


def inventory_sha256(root: Path) -> str:
    """Content identity over every present asset (paths + hashes), including
    Opaque attachments. Stable across retries of an unchanged source."""
    return semantic_sha256(
        [
            {"path": asset["path"], "sha256": asset["sha256"]}
            for asset in inventory_assets(root)
            if asset["presence"] == "present"
        ]
    )


def _format_data(root: Path) -> dict[str, Any] | None:
    path = root / "format.json"
    if not path.is_file() or _is_link(path):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def inspect_workdir(path: str | Path) -> dict[str, Any]:
    """Read-only readiness classification of one schema-1 workdir.

    Returns the inspect data payload: one stable readiness classification
    (``ready`` / ``blocked``), the asset table, stable reason codes, semantic
    state, feature sets, baseline identities, and a source snapshot. Raises
    OSError when the directory is unreadable. Never writes to ``path``.
    """
    root = Path(path).resolve()
    assets = inventory_assets(root)
    present = {
        asset["path"]: asset for asset in assets if asset["presence"] == "present"
    }
    reasons: list[str] = []

    # Symlinks anywhere under the source (including .review and opaque
    # subtrees) block migration with a stable reason; the link itself is
    # never dereferenced by the inventory walk above.
    symlinks = _symlink_paths(root)
    if symlinks:
        reasons.append("symlink-detected")

    missing = [
        name for name, _, _ in AUTHORITATIVE_ASSETS if name not in present
    ]
    if missing:
        reasons.append("asset-closure")

    format_data = _format_data(root)
    schema_ok = (
        format_data is not None
        and format_data.get("schema") == "typed-format-1"
        and format_data.get("model_version") == 1
        and format_data.get("canonicalizer_version") == 1
    )
    if not schema_ok:
        reasons.append("schema-incompatible")

    declared = (format_data or {}).get("required_features")
    if declared is not None:
        if not isinstance(declared, list) or not all(
            isinstance(name, str) for name in declared
        ):
            reasons.append("required-feature-unsupported")
        else:
            unknown = sorted(set(declared) - set(REQUIRED_FEATURES))
            if unknown:
                reasons.append("required-feature-unsupported")

    drift: list[str] = []
    template = root / "_template.docx"
    styles = root / "styles.json"
    if format_data is not None:
        # Drift hashing consults the symlink-safe inventory: a symlinked
        # template/styles is rejected (never hashed through the link).
        if (
            "_template.docx" in present
            and format_data.get("template_sha256")
            and file_sha256(template) != format_data["template_sha256"]
        ):
            drift.append("template")
        if (
            "styles.json" in present
            and format_data.get("styles_sha256")
            and file_sha256(styles) != format_data["styles_sha256"]
        ):
            drift.append("styles")
    if drift:
        reasons.append("source-drift")

    edit_state: dict[str, Any] = {"state": "missing"}
    if not symlinks:  # edit-state classification must never read through a link
        try:
            classified = classify_edit_state(root)
            edit_state = {
                "state": classified["state"],
                "typed_sha256": classified["typed_sha256"],
                "edit_body_sha256": classified["edit_body_sha256"],
            }
        except (TypedError, OSError) as exc:
            if (root / "edit.state.json").exists():
                edit_state = {"state": "incompatible", "detail": str(exc)}
            else:
                edit_state = {"state": "missing", "detail": str(exc)}
    if edit_state["state"] == "missing":
        reasons.append("edit-state-missing")
    elif edit_state["state"] in ("dirty", "stale-clean", "conflict"):
        reasons.append("non-clean-edit")

    opaque_assets = [
        asset for asset in assets if asset["kind"] == "opaque"
    ]
    if opaque_assets:
        reasons.append("opaque-attachment")

    revision_count: int | None = None
    revisions = root / "revisions.json"
    if revisions.is_file() and not _is_link(revisions):
        try:
            inventory = json.loads(revisions.read_text(encoding="utf-8"))
            if isinstance(inventory, dict):
                revision_count = len(inventory.get("revisions", []) or [])
        except (OSError, json.JSONDecodeError):
            revision_count = None

    comment_count: int | None = None
    if format_data is not None:
        comment_count = len(
            [
                record
                for record in format_data.get("paragraphs", [])
                if record.get("part_key") == "comments"
            ]
        )

    blocking = [reason for reason in reasons if reason in BLOCKING_REASONS]
    readiness = "blocked" if blocking else "ready"
    if not reasons:
        reasons.append("ok")

    baseline: dict[str, Any] = {}
    if format_data is not None:
        baseline = {
            "template": format_data.get("template"),
            "template_sha256": format_data.get("template_sha256"),
            "package_manifest": format_data.get("package_manifest"),
            "source": format_data.get("source"),
            "source_sha256": format_data.get("source_sha256"),
            "styles_sha256": format_data.get("styles_sha256"),
            "document_xml_sha256": format_data.get("document_xml_sha256"),
            "source_track_enabled": format_data.get("source_track_enabled"),
            "uses_date_utc": format_data.get("uses_date_utc"),
        }

    present_files = [
        asset for asset in assets if asset["presence"] == "present"
    ]
    return {
        "readiness": readiness,
        "workdir": typed_path(root),
        "next_action": "migrate" if readiness == "ready" else "none",
        "reason_codes": reasons,
        "symlinks": symlinks,
        "semantic_state": {
            "edit": edit_state,
            "template_drift": "template" in drift,
            "styles_drift": "styles" in drift,
            "revision_count": revision_count,
            "comment_count": comment_count,
            "opaque_attachment_count": len(opaque_assets),
        },
        "assets": assets,
        "features": {
            "supported": sorted(FEATURES),
            "required": sorted(REQUIRED_FEATURES),
        },
        "baseline": baseline,
        "source_snapshot": {
            "files": len(present_files),
            "bytes": sum(asset["bytes"] for asset in present_files),
        },
    }


def _blocking_reason(classification: dict[str, Any]) -> str:
    for code in BLOCKING_REASONS:
        if code in classification["reason_codes"]:
            return code
    return "workdir-invalid"


def _copy_assets(source: Path, staging: Path) -> None:
    """Byte-for-byte copy of every present asset, preserving mtimes."""
    for asset in inventory_assets(source):
        if asset["presence"] != "present":
            continue
        rel = Path(asset["path"])
        src = source / rel
        dst = staging / rel
        if src.is_dir():
            shutil.copytree(src, dst)  # copy2 semantics: mtimes preserved
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _verify_staged(
    source: Path,
    staging: Path,
    classification: dict[str, Any],
) -> list[dict[str, str]]:
    """Verify asset closure, byte/semantic equivalence, typed validation, and
    observable behavior of the staged copy before any publication."""
    checks: list[dict[str, str]] = []
    source_assets = {
        asset["path"]: asset
        for asset in inventory_assets(source)
        if asset["presence"] == "present"
    }
    staged_assets = {
        asset["path"]: asset
        for asset in inventory_assets(staging)
        if asset["presence"] == "present"
    }
    if set(source_assets) != set(staged_assets):
        missing = sorted(set(source_assets) - set(staged_assets))
        extra = sorted(set(staged_assets) - set(source_assets))
        raise MigrateError(
            "migrate-verification-failed",
            f"asset closure mismatch after copy; missing={missing} extra={extra}",
        )
    mismatched = [
        path
        for path in source_assets
        if source_assets[path]["sha256"] != staged_assets[path]["sha256"]
    ]
    if mismatched:
        raise MigrateError(
            "migrate-verification-failed",
            f"asset bytes differ after copy: {sorted(mismatched)}",
        )
    checks.append({"name": "asset-closure", "status": "pass"})
    checks.append({"name": "byte-equivalence", "status": "pass"})

    if derived_workdir_manifest(source) != derived_workdir_manifest(staging):
        raise MigrateError(
            "migrate-verification-failed",
            "semantic workdir manifest differs after copy",
        )
    checks.append({"name": "semantic-equivalence", "status": "pass"})

    try:
        validate_workdir(staging)
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        raise MigrateError(
            "migrate-verification-failed",
            f"typed validation of the staged workdir failed: {exc}",
        ) from exc
    checks.append({"name": "typed-validation", "status": "pass"})

    # Observable behavior equivalence: a clean workdir must build byte-identical
    # output from source and from the staged copy. Non-clean semantic state is
    # preserved (never flattened) and recorded in the manifest instead.
    edit_state = classification["semantic_state"]["edit"].get("state")
    if edit_state == "clean":
        scratch = Path(tempfile.mkdtemp(prefix="docx2typed-migrate-verify-"))
        try:
            from_source = build_workdir(source, scratch / "from-source.docx")
            from_staged = build_workdir(staging, scratch / "from-staged.docx")
            if file_sha256(from_source) != file_sha256(from_staged):
                raise MigrateError(
                    "migrate-verification-failed",
                    "observable build output differs between source and staged workdir",
                )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        checks.append({"name": "behavior-equivalence", "status": "pass"})
    else:
        checks.append({"name": "behavior-equivalence", "status": "skipped"})
    return checks


def _build_manifest(
    source: Path,
    staging: Path,
    classification: dict[str, Any],
    *,
    operation_id: str,
    checks: list[dict[str, str]],
) -> dict[str, Any]:
    format_data = _format_data(source) or {}
    asset_fields = (
        "path",
        "kind",
        "required",
        "read_only",
        "role",
        "presence",
        "bytes",
        "sha256",
        "mtime_ns",
    )
    assets = [
        {key: asset[key] for key in asset_fields}
        for asset in classification["assets"]
        if asset["presence"] == "present" and asset["path"] != MANIFEST_FILE
    ]
    # A pre-existing source workdir.manifest.json is excluded above (the
    # copied bytes are overwritten by this generated file later), so the
    # final target declares exactly one manifest entry: this one. The
    # manifest truthfully declares itself as a generated metadata asset
    # without pretending a self-hash: no file can hash or measure the bytes
    # that contain its own declaration, so sha256/bytes/mtime_ns are null
    # with an explicit generated role. The schema encodes this exception.
    assets.append(
        {
            "path": MANIFEST_FILE,
            "kind": "generated",
            "required": True,
            "read_only": True,
            "role": "workdir-manifest",
            "presence": "present",
            "bytes": None,
            "sha256": None,
            "mtime_ns": None,
        }
    )
    manifest = {
        "schema": WORKDIR_MANIFEST_SCHEMA,
        "manifest_version": MANIFEST_VERSION,
        "workdir_schema": format_data.get("schema", "typed-format-1"),
        "model_version": format_data.get("model_version", 1),
        "canonicalizer_version": format_data.get("canonicalizer_version", 1),
        "producer": {
            "engine": "docx2typed-python",
            "version": engine_descriptor()["version"],
            "operation": "migrate",
            "operation_id": operation_id,
        },
        "source": {
            "identity": inventory_sha256(source),
            "semantic_manifest_sha256": semantic_sha256(
                derived_workdir_manifest(source)
            ),
        },
        "baseline": {
            "template": format_data.get("template"),
            "template_sha256": format_data.get("template_sha256"),
            "package_manifest": format_data.get("package_manifest"),
            "source": format_data.get("source"),
            "source_sha256": format_data.get("source_sha256"),
            "styles_sha256": format_data.get("styles_sha256"),
            "document_xml_sha256": format_data.get("document_xml_sha256"),
            "source_track_enabled": format_data.get("source_track_enabled"),
            "uses_date_utc": format_data.get("uses_date_utc"),
        },
        "features": {
            "supported": sorted(FEATURES),
            "required": sorted(REQUIRED_FEATURES),
        },
        "state": {
            "readiness": classification["readiness"],
            "edit": classification["semantic_state"]["edit"],
            "revision_count": classification["semantic_state"]["revision_count"],
            "comment_count": classification["semantic_state"]["comment_count"],
            "reason_codes": classification["reason_codes"],
            "semantic_manifest_sha256": semantic_sha256(
                derived_workdir_manifest(staging)
            ),
        },
        "checks": checks,
        "assets": assets,
    }
    return manifest


def _evidence_payload(
    manifest: dict[str, Any],
    checks: list[dict[str, str]],
    classification: dict[str, Any],
) -> dict[str, Any]:
    opaque = [
        asset
        for asset in classification["assets"]
        if asset["kind"] == "opaque" and asset["presence"] == "present"
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
        "checks": checks,
    }


def migrate_workdir(
    source: str | Path,
    target: str | Path,
    *,
    operation_id: str,
    evidence_path: str | Path,
    on_prepared: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Lossless schema-1 -> manifest-backed migration.

    Reads an immutable snapshot of ``source``, stages every asset byte-for-byte
    in a sibling staging directory, verifies asset closure, semantic state,
    typed validation, and observable behavior, writes the versioned workdir
    manifest, publishes run evidence to ``evidence_path`` (sidecar, before the
    atomic publish), then atomically renames staging onto ``target``.

    ``on_prepared`` (optional) is invoked with the exact success evidence
    once the final manifest is known and the evidence is built, before the
    sidecar is published and before the atomic rename. A JSON-contract caller
    uses it to persist the precomputed success envelope as the pending ledger
    record, so a crash after the publish replays the byte-exact original
    response instead of reconstructing it.

    The schema-1 source is never modified. Any failure removes staging and the
    sidecar evidence and leaves no ``target``. Raises MigrateError with a
    stable Protocol diagnostic code on every blocked or failed step. The
    directory rename is the atomic pointer; durable generation/recovery
    semantics are the later durability ticket.
    """
    source_path = Path(source).resolve()
    target_path = Path(target).resolve()
    if not source_path.is_dir():
        raise MigrateError(
            "workdir-not-found", f"source workdir not found: {source_path}"
        )
    classification = inspect_workdir(source_path)
    if classification["readiness"] != "ready":
        reason = _blocking_reason(classification)
        raise MigrateError(
            reason,
            f"source workdir is not migratable: {', '.join(classification['reason_codes'])}",
        )
    if target_path.exists():
        raise MigrateError(
            "target-already-exists", f"target already exists: {target_path}"
        )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    staging = (
        target_path.parent / f".{target_path.name}.migrate-{uuid.uuid4().hex}.tmp"
    )
    evidence: dict[str, Any] | None = None
    try:
        staging.mkdir()
        _copy_assets(source_path, staging)
        checks = _verify_staged(source_path, staging, classification)
        manifest = _build_manifest(
            source_path,
            staging,
            classification,
            operation_id=operation_id,
            checks=checks,
        )
        (staging / MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        evidence = run_evidence(
            "migrate",
            "success",
            kind="mutation",
            operation_id=operation_id,
            payload=_evidence_payload(manifest, checks, classification),
        )
        if on_prepared is not None:
            # Exact success evidence is known here: hand it to the caller
            # before any publish so the prepared envelope can be persisted.
            on_prepared(evidence)
        publish_run_evidence(evidence_path, evidence)
        os.replace(staging, target_path)  # atomic publish of the whole workdir
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            Path(evidence_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if evidence is None:  # pragma: no cover - run_evidence always returns
        raise MigrateError("migrate-verification-failed", "evidence was not produced")
    return target_path, evidence


# --------------------------------------------------------------------------
# Human CLI
# --------------------------------------------------------------------------

def inspect(argv: list[str] | None = None) -> int:
    """docx2typed inspect — read-only readiness classification of a workdir."""
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="docx2typed inspect",
        description=(
            "Classify one schema-1 typed workdir without modifying it: "
            "readiness, asset table, reason codes, and the permitted next "
            "action. Never creates locks, state files, or evidence."
        ),
    )
    parser.add_argument("source", help="schema-1 typed workdir")
    args = parser.parse_args(argv)
    try:
        data = inspect_workdir(args.source)
    except (OSError, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"workdir:     {data['workdir']['value']}")
    print(f"readiness:   {data['readiness']}")
    print(f"next action: {data['next_action']}")
    print(f"reason codes: {', '.join(data['reason_codes'])}")
    edit_state = data["semantic_state"]["edit"]["state"]
    print(f"edit state:  {edit_state}")
    print(f"revisions:   {data['semantic_state']['revision_count']}")
    print(f"comments:    {data['semantic_state']['comment_count']}")
    print(f"opaque:      {data['semantic_state']['opaque_attachment_count']}")
    print("assets:")
    for asset in data["assets"]:
        sha = (asset["sha256"] or "-")[:12]
        print(
            f"  {asset['path']:<30} {asset['kind']:<14} {asset['presence']:<8} "
            f"required={str(asset['required']):<5} read_only={str(asset['read_only']):<5} "
            f"bytes={asset['bytes']:<8} sha256={sha}"
        )
    return 0


def migrate(argv: list[str] | None = None) -> int:
    """docx2typed migrate — lossless schema-1 -> manifest-backed migration."""
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="docx2typed migrate",
        description=(
            "Copy a schema-1 typed workdir losslessly into a new "
            "manifest-backed workdir. The source is never modified; TARGET is "
            "published atomically only after asset closure, semantic-state, "
            "and observable-behavior verification."
        ),
    )
    parser.add_argument("source", help="schema-1 typed workdir (never modified)")
    parser.add_argument(
        "--out",
        required=True,
        help="target manifest-backed workdir (must not exist)",
    )
    parser.add_argument(
        "--operation-id",
        default=None,
        help="retry identity (default: generated)",
    )
    args = parser.parse_args(argv)
    source = Path(args.source).resolve()
    target = Path(args.out).resolve()
    if not source.exists():
        print(f"ERROR: source workdir not found: {source}")
        return 1
    if target.exists():
        print(f"ERROR: target already exists: {target}")
        return 1
    operation_id = args.operation_id or new_operation_id()
    evidence_path = Path(str(target) + ".migrate.evidence.json")
    try:
        migrated, _ = migrate_workdir(
            source,
            target,
            operation_id=operation_id,
            evidence_path=evidence_path,
        )
    except (OSError, zipfile.BadZipFile, TypedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"migrated: {migrated}")
    print(f"manifest: {migrated / MANIFEST_FILE}")
    print(f"operation id: {operation_id}")
    return 0


if __name__ == "__main__":
    sys.exit(migrate())
