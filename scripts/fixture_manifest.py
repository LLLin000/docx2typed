"""Fixture corpus governance: content-addressed manifest (issue #52).

Schema ``docx2typed-fixture-manifest-2`` records, for EVERY committed
fixture (public, private-pending, calibration): an anonymous id, tier,
sha256, size, provenance/license/consent, generator/toolchain lock,
dialect/features, expected signatures, eligible cells, privacy class,
retention/owner, and mutation lineage.  The coverage section proves the
dialect union demanded by issue #42 (ISO classic, w14-w15-w16-w16du,
classic + modern comments, run/paragraph/move/conflict revisions,
headers/footers/footnotes/endnotes, nested tables/SDT/text boxes, opaque
fields/math/drawings, CJK/fonts/RTL, large/pathological packages) and the
seeded-corruption calibration set with just-inside/just-over S-profile
pairs.  L/X just-inside/just-over packages are generated deterministically
at runtime by scripts/resource_limits.py (documented in ``coverage``).

The manifest is regenerated deterministically by
``python -m scripts.release_fixtures --write-manifest`` and validated by
the ``fixture-corpus`` qualification check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "docx2typed-fixture-manifest-2"
MANIFEST_VERSION = 2
MANIFEST_PATH = "corpus/manifest.json"

TIERS = ("public", "private", "calibration")

# Issue #42 dialect union, proven by at least one committed fixture each.
REQUIRED_DIALECTS = {
    "iso-classic",
    "w14-w15-w16-w16du",
    "classic-comments",
    "modern-comments",
    "run-revisions",
    "paragraph-revisions",
    "move-revisions",
    "conflict-revisions",
    "headers-footers",
    "footnotes-endnotes",
    "nested-tables",
    "sdt-content-controls",
    "text-boxes",
    "opaque-fields-math-drawings",
    "cjk-fonts-rtl",
    "large-packages",
    "pathological-packages",
}

# Seeded-corruption calibration set (issue #42/#38).  Each corruption type
# must be represented; the seven size/scale-sensitive types carry a
# just-inside / just-over S-profile pair (committed), the relationship
# cycle is a single fail-closed probe.
REQUIRED_CORRUPTIONS = {
    "invalid-zip",
    "non-zip-bytes",
    "malformed-document-xml",
    "missing-document-xml",
    "high-compression-duplicate",
    "deep-xml-nesting",
    "oversized-text-node",
    "many-tiny-parts",
    "relationship-cycle",
}

KNOWN_CELLS = {
    "cli.extract",
    "cli.build",
    "cli.validate",
    "cli.verify",
    "mcp.*",
    "office.open",
    "office.render",
    "office.save",
    "office.reopen",
    "office.retention",
    "office.repair-observation",
    "resource.qualification",
}

_REQUIRED_FIELDS = (
    "anonymous_id",
    "tier",
    "path",
    "sha256",
    "size_bytes",
    "provenance",
    "toolchain",
    "dialect",
    "expected_signatures",
    "eligible_cells",
    "privacy",
    "mutation_lineage",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """The fixture manifest is structurally invalid or drifted."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _require_fields(entry: dict[str, Any], path: str) -> None:
    missing = [name for name in _REQUIRED_FIELDS if name not in entry]
    if missing:
        raise ManifestError(f"{path}: missing fields {missing}")
    if not isinstance(entry["anonymous_id"], str) or not entry["anonymous_id"]:
        raise ManifestError(f"{path}: anonymous_id must be a non-empty string")
    if entry["tier"] not in TIERS:
        raise ManifestError(f"{path}: unknown tier {entry['tier']!r}")
    if not isinstance(entry["path"], str) or not entry["path"].startswith(("release/", "calibration/")):
        raise ManifestError(f"{path}: path must live under corpus/release or corpus/calibration")
    if not _HEX64.match(entry["sha256"]):
        raise ManifestError(f"{path}: sha256 must be a lowercase hex digest")
    if not isinstance(entry["size_bytes"], int) or entry["size_bytes"] < 0:
        raise ManifestError(f"{path}: size_bytes must be a non-negative int")
    for sub in ("provenance", "toolchain", "dialect", "privacy"):
        if not isinstance(entry[sub], dict):
            raise ManifestError(f"{path}: {sub} must be an object")
    if not isinstance(entry["eligible_cells"], list) or not entry["eligible_cells"]:
        raise ManifestError(f"{path}: eligible_cells must be a non-empty list")
    unknown_cells = sorted(set(entry["eligible_cells"]) - KNOWN_CELLS)
    if unknown_cells:
        raise ManifestError(f"{path}: unknown eligible cells {unknown_cells}")
    if not isinstance(entry["mutation_lineage"], list) or not entry["mutation_lineage"]:
        raise ManifestError(f"{path}: mutation_lineage must be a non-empty list")
    dialect = entry["dialect"]
    if not isinstance(dialect.get("features"), list) or not dialect["features"]:
        raise ManifestError(f"{path}: dialect.features must be a non-empty list")


def validate_manifest(
    manifest: dict[str, Any],
    root: Path | None = None,
    *,
    check_hashes: bool = True,
) -> dict[str, Any]:
    """Structural + integrity validation of a fixture manifest.

    Returns a detail record; raises ManifestError on structural drift.
    ``check_hashes=False`` skips the on-disk hash audit (used by the plan
    freeze path where files may not be checked out yet).
    """
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"manifest schema {manifest.get('schema')!r} != {MANIFEST_SCHEMA}")
    if manifest.get("version") != MANIFEST_VERSION:
        raise ManifestError(f"manifest version {manifest.get('version')!r} != {MANIFEST_VERSION}")
    _require("generated" in manifest, "manifest needs a generated date")
    tiers = manifest.get("tiers")
    _require(isinstance(tiers, dict) and set(tiers) == set(TIERS), "tiers must cover public/private/calibration")
    fixtures = manifest.get("fixtures")
    _require(isinstance(fixtures, list) and fixtures, "fixtures must be a non-empty list")
    ids: list[str] = []
    for index, entry in enumerate(fixtures):
        _require_fields(entry, f"fixtures[{index}]")
        ids.append(entry["anonymous_id"])
    duplicates = sorted({entry_id for entry_id in ids if ids.count(entry_id) > 1})
    if duplicates:
        raise ManifestError(f"duplicate anonymous ids: {duplicates}")
    coverage = manifest.get("coverage")
    _require(isinstance(coverage, dict), "manifest needs a coverage section")
    inventory = coverage.get("dialect_inventory")
    _require(isinstance(inventory, dict), "coverage.dialect_inventory missing")
    missing_dialects = sorted(REQUIRED_DIALECTS - set(inventory))
    if missing_dialects:
        raise ManifestError(f"coverage misses required dialects: {missing_dialects}")
    calibration = coverage.get("calibration_inventory")
    _require(isinstance(calibration, dict), "coverage.calibration_inventory missing")
    missing_corruptions = sorted(REQUIRED_CORRUPTIONS - set(calibration))
    if missing_corruptions:
        raise ManifestError(f"coverage misses required corruptions: {missing_corruptions}")

    detail: dict[str, Any] = {"schema": MANIFEST_SCHEMA, "fixtures": len(fixtures), "valid": True}
    if check_hashes and root is not None:
        missing_files: list[str] = []
        drifted: list[tuple[str, str, str]] = []
        for entry in fixtures:
            path = (root / "corpus" / entry["path"]).resolve()
            if not path.is_file():
                missing_files.append(entry["path"])
                continue
            from scripts.qualify_adapters import file_sha256

            actual = file_sha256(path)
            if actual != entry["sha256"]:
                drifted.append((entry["path"], entry["sha256"][:12], actual[:12]))
            if path.stat().st_size != entry["size_bytes"]:
                drifted.append((entry["path"], f"size={entry['size_bytes']}", f"size={path.stat().st_size}"))
        if missing_files or drifted:
            detail["valid"] = False
            detail["missing_files"] = missing_files
            detail["drifted"] = drifted
    return detail


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_PATH
    if not path.is_file():
        raise ManifestError(f"missing {MANIFEST_PATH}")
    return json.loads(path.read_text(encoding="utf-8"))


def audit_hashes(manifest: dict[str, Any], root: Path) -> list[tuple[str, str, str]]:
    """Recompute every fixture sha256 against the manifest; returns drifted
    (path, expected, actual) triples."""
    from scripts.qualify_adapters import file_sha256

    drifted: list[tuple[str, str, str]] = []
    for entry in manifest["fixtures"]:
        path = (root / "corpus" / entry["path"]).resolve()
        if not path.is_file():
            drifted.append((entry["path"], entry["sha256"], "missing"))
        else:
            actual = file_sha256(path)
            if actual != entry["sha256"]:
                drifted.append((entry["path"], entry["sha256"], actual))
    return drifted


def build_manifest(
    fixtures: list[dict[str, Any]],
    *,
    coverage: dict[str, Any],
    generated: str,
    private_pending: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a validated manifest document from fixture records."""
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "version": MANIFEST_VERSION,
        "generated": generated,
        "tiers": {
            "public": "deterministic synthetic fixtures, committed, redistributable, public-CI blocking",
            "private": "authorized/minimized documents on controlled runners; never in public artifacts",
            "calibration": "deterministic corruptions and dialect probes validating fail-closed behavior; not product capability claims",
        },
        "fixtures": fixtures,
        "coverage": coverage,
    }
    if private_pending:
        manifest["coverage"]["private_pending"] = private_pending
    validate_manifest(manifest)
    return manifest
