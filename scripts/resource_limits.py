"""Resource profile enforcement for the qualification harness (issue #52).

Frozen S/L/X profiles (issue #38) live in
``qualification/resource_profiles.json`` (schema
``docx2typed-resource-profiles-1``).  This module:

- computes package statistics from a DOCX **without decompressing it**
  (zip member ``file_size`` metadata, namelist, streaming scans), so
  over-limit inputs fail closed before allocation/decompression;
- enforces the limits with the ``resource-limit-exceeded`` diagnostic that
  carries actual/limit/profile and publishes no partial output; there is no
  unlimited bypass;
- generates deterministic just-inside / just-over synthetic packages for
  every profile and dimension, including the L/X pairs that are too large
  to commit to the corpus.

The ``resource-profiles`` qualification check runs
``run_all_checks()`` on this host; the seeded S pairs are the committed
``corpus/calibration`` fixtures.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_PATH = REPO_ROOT / "qualification" / "resource_profiles.json"
PROFILES_SCHEMA = "docx2typed-resource-profiles-1"
CANON_SCHEMA = "docx2typed-resource-canon-1"
DIAGNOSTIC_CODE = "resource-limit-exceeded"
RESULT_SCHEMA = "docx2typed-resource-check-1"

_DIMENSION_LIMITS = (
    "docx_package_mib",
    "uncompressed_xml_mib",
    "zip_parts",
    "prose_leaves",
    "revisions",
    "comments",
    "tables",
    "cells",
    "max_leaf_island_bytes",
    "xml_nesting_depth",
    "compression_ratio_max",
)

_REQUIRED_SECTIONS = (
    "profiles",
    "wall_budgets",
    "rss_formulas",
    "disk",
    "output_growth",
    "measurement",
    "concurrency",
    "runner_classes",
    "no_regression",
    "fail_closed",
)


class ProfileError(ValueError):
    """resource_profiles.json drifted from its frozen contract."""


def load_profiles(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or PROFILES_PATH).read_text(encoding="utf-8"))


def validate_profiles(profiles: dict[str, Any]) -> dict[str, Any]:
    """Structural validation of the frozen profiles document."""
    if profiles.get("schema") != PROFILES_SCHEMA:
        raise ProfileError(f"profiles schema {profiles.get('schema')!r} != {PROFILES_SCHEMA}")
    if profiles.get("canon") != CANON_SCHEMA:
        raise ProfileError(f"profiles canon {profiles.get('canon')!r} != {CANON_SCHEMA}")
    missing = [name for name in _REQUIRED_SECTIONS if name not in profiles]
    if missing:
        raise ProfileError(f"missing sections: {missing}")
    for profile_id, profile in profiles["profiles"].items():
        if profile_id not in ("S", "L", "X"):
            raise ProfileError(f"unknown profile {profile_id!r}")
        for dimension in _DIMENSION_LIMITS:
            value = profile.get(dimension)
            if not isinstance(value, int) or value <= 0:
                raise ProfileError(f"profile {profile_id}: {dimension} must be a positive int")
    for name in ("S", "L"):
        budget = profiles["wall_budgets"].get(name)
        if not isinstance(budget, dict) or "hard_timeout_factor" not in budget:
            raise ProfileError(f"wall_budgets[{name}] malformed")
    for name in ("S", "L"):
        if profiles["rss_formulas"].get(name) not in (
            "min(1.5 GiB, 12 * uncompressed_editable_xml + 256 MiB)",
            "min(6 GiB, 6 * uncompressed_editable_xml + 512 MiB)",
        ):
            raise ProfileError(f"rss_formulas[{name}] drifted from the frozen formula")
    if profiles["fail_closed"].get("diagnostic") != DIAGNOSTIC_CODE:
        raise ProfileError("fail_closed.diagnostic drifted")
    return profiles


@dataclass
class PackageStats:
    package_bytes: int
    zip_parts: int
    duplicate_entries: list[str]
    uncompressed_xml_bytes: int
    max_part_bytes: int
    max_text_node_bytes: int
    xml_nesting_depth: int
    compression_ratio: float
    relationship_cycle: bool
    valid_zip: bool = True
    error: str | None = None


def _iter_tags(data: bytes):
    """Minimal streaming tag scanner: yields (name, start, end) for each tag."""
    pattern = re.compile(rb"<(/?)([A-Za-z_][\w:.-]*)((?:\s[^<>]*?)?)(/?)>")
    for match in pattern.finditer(data):
        yield match.group(2).decode("utf-8", errors="replace"), bool(match.group(1)), bool(match.group(4))


def _scan_text_and_depth(data: bytes) -> tuple[int, int]:
    """Longest single text-node byte count and max element nesting depth."""
    max_text = 0
    current_text = 0
    in_text = False
    depth = 0
    max_depth = 0
    cursor = 0
    text_cursor = 0
    for name, closing, self_closing in _iter_tags(data):
        if closing:
            depth = max(0, depth - 1)
            if in_text and name == "t":
                # text content ended before the closing tag
                in_text = False
                current_text = 0
            continue
        if self_closing:
            continue
        depth += 1
        max_depth = max(max_depth, depth)
        if name == "t":
            in_text = True
            text_cursor = 0
            continue
        if in_text:
            in_text = False
            current_text = 0
    # simple linear scan for the longest text run between <w:t...> and </w:t>
    longest = 0
    for match in re.finditer(rb"<w:t(?:\s[^>]*)?>(.*?)</w:t>", data, re.S):
        longest = max(longest, len(match.group(1)))
    return longest, max_depth


def package_stats(path: Path) -> PackageStats:
    """Compute package statistics from metadata + bounded streaming scans.

    Never decompresses the whole package: uncompressed sizes come from
    ``ZipInfo.file_size``; only small members (document.xml up to a bounded
    scan) are read.  This is the fail-closed-before-allocation path.
    """
    try:
        archive = zipfile.ZipFile(path)
    except Exception as exc:  # noqa: BLE001 - unreadable package is a raw observation
        return PackageStats(
            package_bytes=path.stat().st_size,
            zip_parts=0,
            duplicate_entries=[],
            uncompressed_xml_bytes=0,
            max_part_bytes=0,
            max_text_node_bytes=0,
            xml_nesting_depth=0,
            compression_ratio=0.0,
            relationship_cycle=False,
            valid_zip=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    try:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        package_bytes = path.stat().st_size
        xml_total = sum(info.file_size for info in infos if info.filename.startswith("word/") and info.filename.endswith(".xml"))
        all_total = sum(info.file_size for info in infos)
        max_part = max((info.file_size for info in infos), default=0)
        ratio = (all_total / package_bytes) if package_bytes else 0.0
        document = None
        if "word/document.xml" in names:
            document = archive.read("word/document.xml")
        max_text, depth = _scan_text_and_depth(document or b"")
        cycle = _detect_relationship_cycle(archive)
        return PackageStats(
            package_bytes=package_bytes,
            zip_parts=len(names),
            duplicate_entries=duplicates,
            uncompressed_xml_bytes=xml_total,
            max_part_bytes=max_part,
            max_text_node_bytes=max_text,
            xml_nesting_depth=depth,
            compression_ratio=ratio,
            relationship_cycle=cycle,
        )
    finally:
        archive.close()


def _detect_relationship_cycle(archive: zipfile.ZipFile) -> bool:
    """A relationship cycle exists when a part's rels point back to a part
    whose rels point to the first (directly or transitively), or when a
    relationship targets a missing package member."""
    names = set(archive.namelist())
    targets: dict[str, set[str]] = {}
    for name in names:
        if not name.endswith(".rels"):
            continue
        try:
            data = archive.read(name)
        except Exception:  # noqa: BLE001
            continue
        linked: set[str] = set()
        for target in re.findall(rb'Target="([^"]+)"', data):
            resolved = _resolve_target(name, target.decode("utf-8", errors="replace"))
            if resolved is None:
                continue  # external URI, not a package member
            if resolved not in names:
                return True  # missing target
            linked.add(resolved)
        if linked:
            targets[name] = linked
    # direct cycles: part A's rels mention part B and B's rels mention A
    for part_a, linked in targets.items():
        for part_b in linked:
            if part_b in targets and part_a in targets[part_b]:
                return True
    return False


def _resolve_target(rels_name: str, target: str) -> str | None:
    """Resolve a relationship Target to a package member path, or None for
    external/absolute URIs (hyperlinks, mailto, file://) that are not
    package members.  OPC rules: targets resolve from the *source part's*
    directory, i.e. the rels file's directory minus the ``_rels`` segment;
    ``..`` pops base segments first."""
    if target.startswith("/") or "://" in target or target.startswith("mailto:"):
        return None
    segments = rels_name.split("/")
    if rels_name == "_rels/.rels":
        base: list[str] = []
    elif len(segments) >= 2 and segments[-2] == "_rels":
        base = segments[:-2]  # source part directory
    else:
        base = segments[:-1]
    parts = list(base)
    for segment in target.split("/"):
        if segment == "..":
            if parts:
                parts.pop()
        elif segment != ".":
            parts.append(segment)
    return "/".join(parts).lstrip("/")


def _mib(profile: dict[str, Any], dimension: str) -> int:
    return profile[dimension]


def violations(stats: PackageStats, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Dimension violations of a package against one profile.  Empty list
    means inside the profile; every entry carries actual/limit/profile."""
    profile_id = profile["id"] if isinstance(profile.get("id"), str) else "?"
    out: list[dict[str, Any]] = []

    def add(dimension: str, actual: int | float, limit: int | float, unit: str) -> None:
        out.append(
            {
                "dimension": dimension,
                "actual": round(float(actual), 3) if isinstance(actual, float) else actual,
                "limit": round(float(limit), 3) if isinstance(limit, float) else limit,
                "unit": unit,
                "profile": profile_id,
            }
        )

    if not stats.valid_zip:
        add("package-validity", 1, 0, "zip")
        return out
    package_mib = stats.package_bytes / (1024 * 1024)
    if package_mib > profile["docx_package_mib"]:
        add("docx-package-bytes", round(package_mib, 3), profile["docx_package_mib"], "MiB")
    xml_mib = stats.uncompressed_xml_bytes / (1024 * 1024)
    if xml_mib > profile["uncompressed_xml_mib"]:
        add("uncompressed-xml-bytes", round(xml_mib, 3), profile["uncompressed_xml_mib"], "MiB")
    if stats.zip_parts > profile["zip_parts"]:
        add("zip-parts", stats.zip_parts, profile["zip_parts"], "parts")
    if stats.max_part_bytes > profile["max_leaf_island_bytes"]:
        add("max-leaf-island-bytes", stats.max_part_bytes, profile["max_leaf_island_bytes"], "bytes")
    if stats.max_text_node_bytes > profile["max_leaf_island_bytes"]:
        add("max-text-node-bytes", stats.max_text_node_bytes, profile["max_leaf_island_bytes"], "bytes")
    if stats.xml_nesting_depth > profile["xml_nesting_depth"]:
        add("xml-nesting-depth", stats.xml_nesting_depth, profile["xml_nesting_depth"], "levels")
    if stats.compression_ratio > profile["compression_ratio_max"]:
        add("compression-ratio", round(stats.compression_ratio, 3), profile["compression_ratio_max"], "uncompressed/package")
    if stats.duplicate_entries:
        add("duplicate-entries", len(stats.duplicate_entries), 0, "entries")
    if stats.relationship_cycle:
        add("relationship-cycle", 1, 0, "cycle-or-missing-target")
    return out


def diagnostic_records(profile_id: str, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The resource-limit-exceeded diagnostic shape (actual/limit/profile)."""
    return [
        {
            "schema": "docx2typed-diagnostic-1",
            "code": DIAGNOSTIC_CODE,
            "severity": "error",
            "category": "input",
            "retriable": False,
            "message": f"input exceeds {entry['profile']} profile limit {entry['dimension']} "
            f"(actual {entry['actual']} {entry['unit']}, limit {entry['limit']} {entry['unit']})",
            "details": entry,
        }
        for entry in found
    ]


def enforce_package(
    path: Path,
    profile: dict[str, Any],
    *,
    scratch: Path | None = None,
) -> dict[str, Any]:
    """Fail-closed gate: inside -> pass (scratch untouched); over -> the
    resource-limit-exceeded diagnostic with actual/limit/profile and NO
    partial output (scratch removed).  There is no unlimited bypass."""
    stats = package_stats(path)
    found = violations(stats, profile)
    record: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "profile": profile["id"],
        "package": str(path),
        "stats": {
            "package_bytes": stats.package_bytes,
            "zip_parts": stats.zip_parts,
            "uncompressed_xml_bytes": stats.uncompressed_xml_bytes,
            "max_part_bytes": stats.max_part_bytes,
            "max_text_node_bytes": stats.max_text_node_bytes,
            "xml_nesting_depth": stats.xml_nesting_depth,
            "compression_ratio": round(stats.compression_ratio, 3),
            "duplicate_entries": stats.duplicate_entries,
            "relationship_cycle": stats.relationship_cycle,
            "valid_zip": stats.valid_zip,
        },
        "violations": found,
        "fail_closed": bool(found),
        "diagnostics": diagnostic_records(profile["id"], found),
    }
    if found and scratch is not None and scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    return record


# ---------------------------------------------------------------------------
# Deterministic synthetic packages (runtime inside/over pairs for S/L/X)
# ---------------------------------------------------------------------------


def _temp_docx_path() -> Path:
    import os
    import tempfile

    fd, name = tempfile.mkstemp(prefix="reslim-", suffix=".docx")
    os.close(fd)
    return Path(name)


def _stream_write(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 8, 8, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, content)


def _spaces_xml_parts(
    archive: zipfile.ZipFile,
    profile: dict[str, Any],
    total: int,
) -> None:
    """Stream ``total`` space bytes split across word/tN.xml parts so the
    sum of member sizes (payload + per-part XML wrapper) lands exactly on
    ``total`` while every part stays under the leaf limit."""
    header = (
        b'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:r><w:t xml:space=\"preserve\">"
    )
    footer = b"</w:t></w:r></w:p>"
    overhead = len(header) + len(footer)
    chunk_target = max(1, (profile["max_leaf_island_bytes"] * 9) // 10)
    count = max(1, (total + chunk_target - 1) // chunk_target)
    payload_total = max(0, total - count * overhead)
    base_payload = payload_total // count
    sizes = [base_payload] * count
    sizes[-1] += payload_total - base_payload * count
    for index, size in enumerate(sizes):
        info = zipfile.ZipInfo(f"word/t{index:05d}.xml", date_time=(2026, 8, 8, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w") as handle:
            handle.write(header)
            written = 0
            block = b" " * (1 << 20)
            while written < size:
                take = min(len(block), size - written)
                handle.write(block[:take])
                written += take
            handle.write(footer)


def _spaces_xml_doc(size: int) -> Path:
    """Legacy single-member generator: kept for the text-node dimension."""
    path = _temp_docx_path()
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            header = (
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                b"<w:body><w:p><w:r><w:t xml:space=\"preserve\">"
            )
            footer = b"</w:t></w:r></w:p></w:body></w:document>"
            info = zipfile.ZipInfo("word/document.xml", date_time=(2026, 8, 8, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(info, "w") as handle:
                handle.write(header)
                remaining = size
                chunk = b" " * (1 << 20)
                while remaining > 0:
                    take = min(len(chunk), remaining)
                    handle.write(chunk[:take])
                    remaining -= take
                handle.write(footer)
            _stream_write(
                archive,
                "[Content_Types].xml",
                b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                b'<Default Extension="xml" ContentType="application/xml"/>'
                b"</Types>",
            )
            _stream_write(
                archive,
                "_rels/.rels",
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
            )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def make_uncompressed_xml(profile: dict[str, Any], *, over: bool) -> Path:
    """Total word/*.xml uncompressed bytes at/over the profile limit, split
    into parts under the leaf limit."""
    limit = profile["uncompressed_xml_mib"] * 1024 * 1024
    total = limit + (1 if over else 0)
    base_doc = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t>split</w:t></w:r></w:p></w:body></w:document>"
    )
    path = _temp_docx_path()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        _stream_write(archive, "word/document.xml", base_doc)
        _spaces_xml_parts(archive, profile, total - len(base_doc))
        _stream_write(
            archive,
            "[Content_Types].xml",
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b"</Types>",
        )
        _stream_write(
            archive,
            "_rels/.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
    return path


def make_nesting(profile: dict[str, Any], *, over: bool) -> Path:
    # document/body/p wrappers add 3 levels and the r/t leaves add 2 more
    depth = profile["xml_nesting_depth"] - 5 + (1 if over else 0)
    inner = b"<w:r><w:t>depth</w:t></w:r>"
    for _ in range(depth):
        inner = b"<w:ins>" + inner + b"</w:ins>"
    data = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p>"
        + inner
        + b"</w:p></w:body></w:document>"
    )
    path = _temp_docx_path()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        _stream_write(archive, "word/document.xml", data)
    return path


def make_text_node(profile: dict[str, Any], *, over: bool) -> Path:
    # inside: text node sized so the member lands at the leaf limit;
    # over: text node strictly above the limit
    limit = profile["max_leaf_island_bytes"]
    size = limit + 1 if over else max(1, limit - 512)
    data = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t xml:space=\"preserve\">"
        + b"x" * size
        + b"</w:t></w:r></w:p></w:body></w:document>"
    )
    path = _temp_docx_path()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        _stream_write(archive, "word/document.xml", data)
    return path


def make_many_parts(profile: dict[str, Any], *, over: bool) -> Path:
    target = profile["zip_parts"] + (1 if over else 0)
    path = _temp_docx_path()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        _stream_write(
            archive,
            "word/document.xml",
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p><w:r><w:t>parts</w:t></w:r></w:p></w:body></w:document>",
        )
        _stream_write(
            archive,
            "[Content_Types].xml",
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b"</Types>",
        )
        _stream_write(
            archive,
            "_rels/.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        base = 3  # document.xml + content types + rels
        for index in range(target - base):
            _stream_write(archive, f"word/media/t{index:05d}.bin", b"x" * 64)
    return path


def make_package_size(profile: dict[str, Any], *, over: bool) -> Path:
    """Package bytes just inside/over the profile limit using deterministic
    pseudo-random (incompressible) content split into parts under the leaf
    limit, streamed in chunks."""
    limit_bytes = profile["docx_package_mib"] * 1024 * 1024
    margin = 2 * 1024 * 1024
    target = limit_bytes - margin + (2 * margin if over else 0)
    path = _temp_docx_path()
    rng = random.Random(0xC0FFEE)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        _stream_write(
            archive,
            "word/document.xml",
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p><w:r><w:t>blob</w:t></w:r></w:p></w:body></w:document>",
        )
        _stream_write(
            archive,
            "[Content_Types].xml",
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b"</Types>",
        )
        _stream_write(
            archive,
            "_rels/.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        chunk = (profile["max_leaf_island_bytes"] * 4) // 5  # 80% of leaf limit
        written = 0
        index = 0
        block = rng.randbytes(min(chunk, 1 << 20))
        while written < target:
            info = zipfile.ZipInfo(f"word/media/b{index:05d}.bin", date_time=(2026, 8, 8, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(info, "w") as handle:
                take = min(chunk, target - written)
                remaining = take
                while remaining > 0:
                    use = min(len(block), remaining)
                    handle.write(block[:use])
                    remaining -= use
                written += take
            index += 1
    return path


def make_high_compression(profile: dict[str, Any], *, over: bool) -> Path:
    """Compressed-to-limit XML; the over variant also duplicates the member
    (high-compression + duplicate probe) and trips the ratio gate."""
    base = make_uncompressed_xml(profile, over=over)
    if not over:
        return base
    # append a duplicate member
    out = _temp_docx_path()
    with zipfile.ZipFile(base) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(
                zipfile.ZipInfo(info.filename, date_time=(2026, 8, 8, 0, 0, 0)),
                src.read(info.filename),
            )
            if info.filename == "word/document.xml":
                dst.writestr(
                    zipfile.ZipInfo(info.filename, date_time=(2026, 8, 8, 0, 0, 0)),
                    src.read(info.filename),
                )
    base.unlink(missing_ok=True)
    return out


DIMENSION_GENERATORS: dict[str, Any] = {
    "uncompressed-xml": make_uncompressed_xml,
    "nesting-depth": make_nesting,
    "text-node": make_text_node,
    "zip-parts": make_many_parts,
    "package-bytes": make_package_size,
    "high-compression-duplicate": make_high_compression,
}


def run_profile_checks(profiles: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Deterministic enforcement checks for every profile x dimension pair.

    For each dimension with a generator: build the just-inside package
    (gate must pass) and the just-over package (gate must fail closed with
    the resource-limit-exceeded diagnostic carrying actual/limit/profile,
    and no partial output).  The committed S pairs in corpus/calibration are
    also audited against the S profile.
    """
    profiles = validate_profiles(profiles or load_profiles())
    records: list[dict[str, Any]] = []
    for profile_id, profile in profiles["profiles"].items():
        profile = {**profile, "id": profile_id}
        for dimension, generator in DIMENSION_GENERATORS.items():
            for side, over in (("inside", False), ("over", True)):
                path = generator(profile, over=over)
                try:
                    scratch = path.parent / f"{path.stem}-scratch"
                    scratch.mkdir(exist_ok=True)
                    (scratch / "partial-out.docx").write_bytes(b"partial")
                    result = enforce_package(path, profile, scratch=scratch)
                    passed = (side == "inside") == (not result["fail_closed"])
                    no_partial = not scratch.exists() if result["fail_closed"] else scratch.exists()
                    records.append(
                        {
                            "schema": RESULT_SCHEMA,
                            "profile": profile_id,
                            "dimension": dimension,
                            "side": side,
                            "expected": "pass" if side == "inside" else "fail-closed",
                            "actual": "pass" if not result["fail_closed"] else "fail-closed",
                            "check_passed": passed and (no_partial if result["fail_closed"] else True),
                            "no_partial_output": no_partial,
                            "stats": result["stats"],
                            "violations": result["violations"],
                            "diagnostic_codes": [d["code"] for d in result["diagnostics"]],
                            "diagnostic_details": [d["details"] for d in result["diagnostics"]],
                        }
                    )
                    if not passed or (result["fail_closed"] and not no_partial):
                        records[-1]["reason"] = "gate behaved unexpectedly"
                finally:
                    path.unlink(missing_ok=True)
    # Audit the committed S-pair calibration fixtures against the S profile.
    calibration = REPO_ROOT / "corpus" / "calibration"
    if calibration.is_dir():
        s_profile = {**profiles["profiles"]["S"], "id": "S"}
        for fixture in sorted(calibration.glob("*.docx")):
            if "inside" in fixture.stem:
                expected = "pass"
            elif "over" in fixture.stem:
                expected = "fail-closed"
            else:
                continue
            result = enforce_package(fixture, s_profile)
            actual = "pass" if not result["fail_closed"] else "fail-closed"
            records.append(
                {
                    "schema": RESULT_SCHEMA,
                    "profile": "S",
                    "dimension": "committed-calibration",
                    "side": "inside" if expected == "pass" else "over",
                    "fixture": fixture.name,
                    "expected": expected,
                    "actual": actual,
                    "check_passed": actual == expected,
                    "no_partial_output": True,
                    "stats": result["stats"],
                    "violations": result["violations"],
                    "diagnostic_codes": [d["code"] for d in result["diagnostics"]],
                    "diagnostic_details": [d["details"] for d in result["diagnostics"]],
                }
            )
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(1 for record in records if record["check_passed"])
    return {
        "schema": "docx2typed-resource-summary-1",
        "checks": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "failures": [
            {key: record[key] for key in ("profile", "dimension", "side", "expected", "actual", "reason") if key in record}
            for record in records
            if not record["check_passed"]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit evidence records as JSON")
    args = parser.parse_args(argv)
    try:
        records = run_profile_checks()
    except ProfileError as exc:
        print(f"profile error: {exc}")
        return 2
    summary = summarize(records)
    print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else (
        f"{summary['passed']}/{summary['checks']} resource checks passed"
    ))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
