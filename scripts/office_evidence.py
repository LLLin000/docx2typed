"""Phase-separated Office consumer evidence harness (issue #52).

Productionization of the #43 prototype
(prototypes/office_qualification_probe.py, commit 3c9da24).  For every
blocking consumer x fixture x phase combination the harness records a
separate verdict: open, PDF render, save-as DOCX, reopen, semantic
retention, and (Word-only) repair-warning observation.

Design rules inherited from the prototype findings:

- LibreOffice exit code + artifact is NOT package-validity evidence (LO
  emits a PDF for arbitrary non-ZIP bytes).  Every LO cell is paired with
  project package/XML prevalidation before and after, an isolated profile
  per phase, and an independent semantic roundtrip signature.
- Word COM runs phase-isolated (one PowerShell process per phase) with a
  per-phase watchdog, spawned-process cleanup, a session-health preflight,
  and runner quarantine after any timeout (never retried in place).
- Word exposes no programmatic ``repaired`` signal, so repair-free cannot
  be claimed by automation: repair-observation cells are recorded
  not-run (human interactive observation required) and BLOCK the gate.
- Roundtrip retention uses a versioned semantic signature with named,
  consumer-owned rewrite rules — never DOCX byte equality.
- Seeded-corruption calibration runs FIRST and calibrates each adapter.
- Evidence revisions are immutable: ``--collect`` always writes a new
  ``qualification/evidence/rev-<N>/`` directory and never overwrites.
- macOS/Linux/WPS/LTSC cells that cannot run on this host are recorded
  not-run with reasons; ``--verify`` fails closed on any blocking cell
  that is not pass.

Usage:
    python -m scripts.office_evidence --collect            # run + write rev-N
    python -m scripts.office_evidence --verify             # fail-closed gate
    python -m scripts.office_evidence --matrix             # print matrix
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_ROOT = REPO_ROOT / "qualification" / "evidence"

EVIDENCE_SCHEMA = "docx2typed-office-evidence-1"
SEMANTIC_SCHEMA = "docx2typed-office-semantic-1"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

WORD_PATH = Path(r"C:/Program Files/Microsoft Office/root/Office16/WINWORD.EXE")
SOFFICE_PATH = Path(r"C:/Program Files/LibreOffice/program/soffice.exe")

PHASES_LO = ("open", "render", "save", "reopen", "retention")
PHASES_WORD = ("open", "render", "save", "reopen", "retention", "repair-observation")
PHASE_TIMEOUT_SECONDS = 120
SESSION_PREFLIGHT_TIMEOUT = 40

# Consumer declarations (issue #42 matrix).  ``pinned`` means the exact build
# is recorded in every evidence revision; ``available`` is host-determined.
CONSUMERS: dict[str, dict[str, Any]] = {
    "word-windows-m365": {
        "family": "word",
        "platform": "windows",
        "build": "16.0.20228.20158",
        "version": "16.0",
        "role": "blocking",
        "phases": list(PHASES_WORD),
        "adapter": "word-windows",
        "pinned": True,
        "license_note": "licensed Microsoft 365 desktop, logged-in session",
    },
    "word-ltsc-2024": {
        "family": "word",
        "platform": "windows",
        "build": "16.0.17830 (required)",
        "version": "16.0",
        "role": "blocking",
        "phases": list(PHASES_WORD),
        "adapter": None,  # not installed on this host
        "pinned": False,
        "license_note": "Office LTSC 2024 Word; not installed on this runner",
    },
    "word-macos-m365": {
        "family": "word",
        "platform": "macos",
        "build": "latest M365 Current (required)",
        "version": "16.x",
        "role": "blocking",
        "phases": list(PHASES_WORD),
        "adapter": "word-macos",  # no licensed macOS host on this runner
        "pinned": False,
        "license_note": "requires a licensed macOS Word host; not supplied on this runner",
    },
    "lo-fresh-linux": {
        "family": "libreoffice",
        "platform": "linux",
        "build": "current Fresh minor (required)",
        "version": "26.x",
        "role": "blocking",
        "phases": list(PHASES_LO),
        "adapter": "libreoffice-linux",  # no Linux qualification host here
        "pinned": False,
        "license_note": "requires a Linux x86_64 qualification host; not supplied on this runner",
    },
    "lo-still-linux": {
        "family": "libreoffice",
        "platform": "linux",
        "build": "previous supported Still line (required)",
        "version": "25.x",
        "role": "blocking",
        "phases": list(PHASES_LO),
        "adapter": "libreoffice-linux",
        "pinned": False,
        "license_note": "requires a Linux x86_64 qualification host; not supplied on this runner",
    },
    "lo-windows": {
        "family": "libreoffice",
        "platform": "windows",
        "build": "26.2.2.2",
        "version": "26.2.2.2",
        "role": "blocking-platform-integration",
        "phases": list(PHASES_LO),
        "adapter": "libreoffice",
        "pinned": True,
        "license_note": "LibreOffice on Windows is blocking for platform integration",
    },
    "wps-windows": {
        "family": "wps",
        "platform": "windows",
        "build": "current WPS on Windows (best-effort)",
        "version": "unknown",
        "role": "best-effort",
        "phases": list(PHASES_LO),
        "adapter": "wps",
        "pinned": False,
        "license_note": "WPS results are versioned best-effort; not installed on this runner",
    },
}

# Roles whose cells are part of the fail-closed blocking matrix (issue #42).
BLOCKING_ROLES = ("blocking", "blocking-platform-integration")

# Named, versioned consumer-owned rewrite rules for semantic retention.
# ``exact`` fields must match byte-for-byte after canonical sorting;
# ``relaxed`` fields are compared with the documented tolerance;
# ``ignored`` names what the signature never reads (zip layout/timestamps,
# rsid churn, app/core properties) — there is no generic "Office changed it".
CONSUMER_REWRITE_RULES: dict[str, dict[str, Any]] = {
    "word-windows-m365": {
        "rule_id": "word-windows-2026-08-1",
        "exact": [
            "visible_text_sha256",
            "deleted_text_sha256",
            "revision_marks",
            "comments",
            "relationships",
        ],
        "relaxed": {
            "bookmarks": "Word adds navigation bookmarks (e.g. _GoBack) on open; all before bookmarks must survive",
            "comment_parts": "Word may upgrade comment part graphs; all before comment parts must survive",
        },
        "ignored": ["zip_layout_compression_timestamps", "rsid_churn", "app_core_properties"],
    },
    "lo-windows": {
        "rule_id": "lo-windows-2026-08-1",
        "exact": [
            "visible_text_sha256",
            "deleted_text_sha256",
            "comments",
            "comment_parts",
            "bookmarks",
        ],
        "relaxed": {
            "revision_marks": "LO rewrites revision container structure (prototype: count 2->3); "
            "retention requires every before (kind,id) present in after",
            "relationships": "LO prunes/rewrites package relationships on save; retention requires "
            "every external hyperlink/mailto relationship target to survive",
        },
        "ignored": ["zip_layout_compression_timestamps", "rsid_churn", "app_core_properties"],
    },
    "wps-windows": {
        "rule_id": "wps-windows-2026-08-1",
        "exact": [
            "visible_text_sha256",
            "deleted_text_sha256",
            "revision_marks",
            "comments",
        ],
        "relaxed": {
            "bookmarks": "WPS adds navigation bookmarks (e.g. _GoBack) on save; all before bookmarks must survive",
            "comment_parts": "WPS upgrades classic comment graphs to modern parts (commentsExtended/people) on save; all before comment parts must survive",
            "relationships": "WPS prunes/rewrites package relationships on save; retention requires "
            "every external hyperlink/mailto relationship target to survive",
        },
        "ignored": ["zip_layout_compression_timestamps", "rsid_churn", "app_core_properties"],
    },
}

# The four seeded corruptions used to calibrate every adapter (subset of the
# committed calibration corpus).
CALIBRATION_FIXTURES = (
    "cal-invalid-zip.docx",
    "cal-non-zip-bytes.docx",
    "cal-malformed-document-xml.docx",
    "cal-missing-document-xml.docx",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Semantic signature (versioned, consumer-owned rewrite rules)
# ---------------------------------------------------------------------------


def semantic_signature(path: Path) -> dict[str, Any]:
    """Independent semantic signature; never byte equality."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(archive.namelist())
            xml = {
                name: archive.read(name)
                for name in names
                if name.startswith("word/") and name.endswith(".xml")
            }
            rels = {
                name: archive.read(name)
                for name in names
                if name.endswith(".rels")
            }
    except (OSError, zipfile.BadZipFile) as exc:
        return {"schema": SEMANTIC_SCHEMA, "valid_zip": False, "error": str(exc)}
    visible: list[str] = []
    deleted: list[str] = []
    for value in xml.values():
        visible.extend(
            match.decode("utf-8", errors="replace")
            for match in re.findall(rb"<w:t[^>]*>(.*?)</w:t>", value, re.S)
        )
        deleted.extend(
            match.decode("utf-8", errors="replace")
            for match in re.findall(rb"<w:delText[^>]*>(.*?)</w:delText>", value, re.S)
        )
    blob = b"".join(xml.values())
    revision_marks = sorted(
        _revision_mark_identity(match)
        for match in re.finditer(
            rb"<w:(ins|del|moveFrom|moveTo)\b[^>]*>", blob
        )
        if match.group(1) in (b"ins", b"del", b"moveFrom", b"moveTo")
    )
    comments: list[str] = []
    comments_xml = xml.get("word/comments.xml", b"")
    for block in re.findall(rb"<w:comment\b[^>]*>.*?</w:comment>", comments_xml, re.S):
        head = re.match(rb'<w:comment\b[^>]*\sw:id="(\d+)"[^>]*\sw:author="([^"]*)"', block)
        if not head:
            continue
        comment_id = head.group(1).decode("utf-8", errors="replace")
        author = head.group(2).decode("utf-8", errors="replace")
        text = "".join(
            match.decode("utf-8", errors="replace")
            for match in re.findall(rb"<w:t[^>]*>(.*?)</w:t>", block, re.S)
        )
        comments.append(f"{comment_id}:{author}:{text}")
    comment_parts = sorted(
        name for name in names if any(token in name.lower() for token in ("comment", "people"))
    )
    bookmarks = sorted(
        {
            name.decode("utf-8", errors="replace")
            for name in re.findall(rb"<w:bookmarkStart[^>]*\sw:name=\"([^\"]*)\"", blob)
        }
    )
    relationships: list[str] = []
    for rels_name, rels in rels.items():
        for rel_id, rel_type, rel_target in re.findall(
            rb'<Relationship\s+Id="([^"]+)"\s+Type="([^"]+)"\s+Target="([^"]*)"', rels
        ):
            relationships.append(
                f"{rel_id.decode('utf-8', errors='replace')}:"
                f"{rel_type.decode('utf-8', errors='replace')}:"
                f"{rel_target.decode('utf-8', errors='replace')}"
            )
    return {
        "schema": SEMANTIC_SCHEMA,
        "valid_zip": True,
        "visible_text_sha256": hashlib.sha256("".join(visible).encode("utf-8")).hexdigest(),
        "visible_text_chars": len("".join(visible)),
        "deleted_text_sha256": hashlib.sha256("".join(deleted).encode("utf-8")).hexdigest(),
        "revision_marks": revision_marks,
        "comments": comments,
        "comment_parts": comment_parts,
        "bookmarks": bookmarks,
        "relationships": sorted(relationships),
    }


def _revision_mark_identity(match) -> str:
    """Content-based revision identity: kind:author:date — w:id is a local
    dialect field (CONTEXT.md), never a global key; consumers renumber ids
    freely, so retention compares the governed content."""
    tag = match.group(1).decode("utf-8", errors="replace")
    kind = {"ins": "insert", "del": "delete", "moveFrom": "move_from", "moveTo": "move_to"}.get(tag, tag)
    attrs = match.group(0)
    def attr(name: str) -> str:
        found = re.search(rb"w:" + name.encode() + rb'="([^"]*)"', attrs)
        return found.group(1).decode("utf-8", errors="replace") if found else ""
    return f"{kind}:{attr('author')}:{attr('date')}"


def _external_rel_pairs(rels: set[str]) -> set[tuple[str, str]]:
    """(type, target) pairs of external hyperlink/mailto relationships;
    relationship Ids are consumer-owned and never part of the identity."""
    out: set[tuple[str, str]] = set()
    for rel in rels:
        parts = rel.split(":", 2)
        if len(parts) != 3:
            continue
        rel_type, target = parts[1], parts[2]
        if "hyperlink" in rel_type or target.startswith(("http", "mailto")):
            out.add((rel_type, target))
    return out


def evaluate_retention(before: dict[str, Any], after: dict[str, Any], consumer_id: str) -> dict[str, Any]:
    """Apply the consumer's named rewrite rules; returns retained + diffs."""
    rule = CONSUMER_REWRITE_RULES.get(consumer_id)
    if rule is None:
        rule = {"rule_id": "default-strict-1", "exact": [k for k in before if k != "schema"], "relaxed": {}, "ignored": []}
    diffs: list[str] = []
    for key in rule["exact"]:
        if before.get(key) != after.get(key):
            diffs.append(f"{key} changed")
    for key, tolerance in rule["relaxed"].items():
        before_set = set(before.get(key, []))
        after_set = set(after.get(key, []))
        if key == "relationships":
            # external hyperlink/mailto targets must survive consumer churn;
            # relationship Ids are consumer-owned and may change
            external_before = _external_rel_pairs(before_set)
            external_after = _external_rel_pairs(after_set)
            missing = external_before - external_after
            if missing:
                diffs.append(f"relationships: external targets lost ({sorted(missing)[:3]})")
            continue
        if key in ("bookmarks", "comment_parts", "revision_marks"):
            # consumer may add navigation bookmarks / upgrade comment part
            # graphs / churn revision containers; every before item must
            # survive
            missing = before_set - after_set
            if missing:
                diffs.append(f"{key}: before items not all retained ({sorted(missing)[:3]})")
            continue
        if not before_set <= after_set:
            diffs.append(f"{key}: before marks not all retained ({tolerance})")
    return {
        "rule_id": rule["rule_id"],
        "retained": not diffs,
        "diffs": diffs,
        "before": {key: before.get(key) for key in ("visible_text_sha256", "revision_marks", "comments", "comment_parts", "bookmarks", "relationships") if key in before},
        "after": {key: after.get(key) for key in ("visible_text_sha256", "revision_marks", "comments", "comment_parts", "bookmarks", "relationships") if key in after},
    }


# ---------------------------------------------------------------------------
# Prevalidation (project package/XML validity, never Office exit status)
# ---------------------------------------------------------------------------


def prevalidate(path: Path) -> dict[str, Any]:
    """Package/XML prevalidation: zip structure, document.xml presence and
    well-formedness, no resource-profile violations (S profile)."""
    from scripts.resource_limits import enforce_package, load_profiles, validate_profiles

    profiles = validate_profiles(load_profiles())
    s_profile = {**profiles["profiles"]["S"], "id": "S"}
    result = enforce_package(path, s_profile)
    checks: dict[str, Any] = {
        "valid_zip": result["stats"]["valid_zip"],
        "document.xml_present": False,
        "document.xml_wellformed": False,
        "resource_violations": result["violations"],
        "fail_closed": result["fail_closed"],
    }
    if checks["valid_zip"]:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            checks["document.xml_present"] = "word/document.xml" in names
            if checks["document.xml_present"]:
                import xml.etree.ElementTree as ET

                try:
                    ET.fromstring(archive.read("word/document.xml"))
                    checks["document.xml_wellformed"] = True
                except ET.ParseError as exc:
                    checks["document.xml_parse_error"] = str(exc)
    checks["valid"] = (
        checks["valid_zip"]
        and checks["document.xml_present"]
        and checks["document.xml_wellformed"]
        and not checks["resource_violations"]
    )
    return checks


# ---------------------------------------------------------------------------
# LibreOffice adapter (isolated profile per phase)
# ---------------------------------------------------------------------------


def _lo_version() -> str:
    if SOFFICE_PATH.exists():
        try:
            import win32api  # type: ignore  # noqa: PLC0415

            info = win32api.GetFileVersionInfo(str(SOFFICE_PATH), "\\")
            return f"{info['FileVersionMS'] >> 16}.{info['FileVersionMS'] & 0xFFFF}.{info['FileVersionLS'] >> 16}.{info['FileVersionLS'] & 0xFFFF}"
        except Exception:  # noqa: BLE001
            return "26.2.2.2"
    return "unknown"


def _lo_run(args: list[str], timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        run = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
        return {
            "exit_code": run.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "timed_out": False,
            "stdout_tail": (run.stdout or "")[-800:],
            "stderr_tail": (run.stderr or "")[-800:],
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": None,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "timed_out": True,
            "stdout_tail": "",
            "stderr_tail": "timeout",
        }


def _lo_convert(fixture: Path, out_dir: Path, *, target: str, phase_dir: Path) -> dict[str, Any]:
    profile = phase_dir / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    common = [
        str(SOFFICE_PATH), "--headless", "--norestore", "--nolockcheck", "--nofirststartwizard",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
    ]
    # LO rejects backslash paths on Windows; always pass forward slashes.
    fixture_arg = fixture.resolve().as_posix()
    out_arg = out_dir.resolve().as_posix()
    if target == "pdf":
        raw = _lo_run(common + ["--convert-to", "pdf:writer_pdf_Export", "--outdir", out_arg, fixture_arg], 120)
        artifact = out_dir / f"{fixture.stem}.pdf"
    else:
        raw = _lo_run(common + ["--convert-to", "docx:Office Open XML Text", "--outdir", out_arg, fixture_arg], 180)
        artifact = out_dir / fixture.name
    return {**raw, "artifact": str(artifact), "artifact_exists": artifact.exists()}


def run_lo_phases(fixture: Path, work: Path, consumer_id: str) -> list[dict[str, Any]]:
    """LO open/render/save/reopen/retention phases with isolated profiles."""
    out = work / consumer_id / fixture.stem
    out.mkdir(parents=True, exist_ok=True)
    cells: list[dict[str, Any]] = []
    pre_before = prevalidate(fixture)
    before = semantic_signature(fixture)

    def cell(phase: str, result: str, reason: str = "", **extra: Any) -> dict[str, Any]:
        return {
            "consumer": consumer_id,
            "fixture": fixture.name,
            "fixture_sha256": sha256(fixture),
            "phase": phase,
            "result": result,
            "reason": reason,
            **extra,
        }

    if not pre_before["valid"]:
        reason = f"prevalidation failed before consumer: {pre_before}"
        return [cell(p, "fail", reason) for p in PHASES_LO]
    # open + render share the PDF conversion (LO headless has no separate open)
    pdf_dir = out / "pdf-open"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    raw = _lo_convert(fixture, pdf_dir, target="pdf", phase_dir=out / "p-open")
    pdf = Path(raw["artifact"])
    opened = raw["exit_code"] == 0 and raw["artifact_exists"]
    cells.append(cell("open", "pass" if opened else "fail", reason=f"rc={raw['exit_code']} artifact={raw['artifact_exists']}", **{"process": raw}))
    pages = 0
    text_chars = 0
    if pdf.exists():
        try:
            from pypdf import PdfReader  # type: ignore  # noqa: PLC0415

            reader = PdfReader(str(pdf))
            pages = len(reader.pages)
            text_chars = sum(len((page.extract_text() or "")) for page in reader.pages)
        except Exception as exc:  # noqa: BLE001
            cells.append(cell("render", "fail", reason=f"pdf unreadable: {exc}"))
            pdf_ok = False
        else:
            pdf_ok = pages >= 1 and text_chars > 0
            cells.append(
                cell("render", "pass" if pdf_ok else "fail", reason=f"pages={pages} text_chars={text_chars}",
                     **{"pdf_pages": pages, "pdf_text_chars": text_chars, "pdf_sha256": sha256(pdf) if pdf.exists() else None})
            )
    else:
        pdf_ok = False
        cells.append(cell("render", "fail", reason="no pdf artifact"))
    # save
    docx_dir = out / "docx-save"
    docx_dir.mkdir(parents=True, exist_ok=True)
    raw_save = _lo_convert(fixture, docx_dir, target="docx", phase_dir=out / "p-save")
    saved = Path(raw_save["artifact"])
    saved_ok = raw_save["exit_code"] == 0 and raw_save["artifact_exists"]
    cells.append(cell("save", "pass" if saved_ok else "fail", reason=f"rc={raw_save['exit_code']} artifact={raw_save['artifact_exists']}", **{"process": raw_save, "artifact_sha256": sha256(saved) if saved.exists() else None}))
    # reopen: convert the saved docx to PDF again
    reopen_dir = out / "pdf-reopen"
    reopen_dir.mkdir(parents=True, exist_ok=True)
    if saved.exists():
        raw_reopen = _lo_convert(saved, reopen_dir, target="pdf", phase_dir=out / "p-reopen")
        reopened_pdf = Path(raw_reopen["artifact"])
        reopen_ok = raw_reopen["exit_code"] == 0 and raw_reopen["artifact_exists"]
        cells.append(cell("reopen", "pass" if reopen_ok else "fail", reason=f"rc={raw_reopen['exit_code']} artifact={raw_reopen['artifact_exists']}", **{"process": raw_reopen}))
    else:
        reopen_ok = False
        cells.append(cell("reopen", "fail", reason="no saved docx to reopen"))
    # retention
    after = semantic_signature(saved) if saved.exists() else {"schema": SEMANTIC_SCHEMA, "valid_zip": False}
    retention = evaluate_retention(before, after, consumer_id)
    pre_after = prevalidate(saved) if saved.exists() else {"valid": False}
    cells.append(
        cell(
            "retention",
            "pass" if retention["retained"] and pre_after["valid"] else "fail",
            reason=(
                f"rule={retention['rule_id']} retained={retention['retained']} "
                f"diffs={retention['diffs'][:4]} post_valid={pre_after['valid']}"
            ),
            **{"semantic_before": before, "semantic_after": after, "retention": retention, "post_prevalidation": pre_after},
        )
    )
    return cells


# ---------------------------------------------------------------------------
# Word COM adapter (phase-isolated, watchdog, quarantine)
# ---------------------------------------------------------------------------

WORD_PHASE_SCRIPTS = {
    "open": r"""
param([string]$InputPath)
$ErrorActionPreference = "Stop"
$word = $null; $doc = $null
$result = [ordered]@{open=$false; error=$null; version=$null; build=$null}
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $result.version = $word.Version
  $result.build = $word.Build
  $doc = $word.Documents.Open($InputPath, $false, $true, $false, "", "", $false, "", "", 0, $null, $false, $false, $false, $true)
  $result.open = $true
} catch {
  $result.error = $_.Exception.Message
} finally {
  if ($doc -ne $null) { try { $doc.Close($false) } catch {} }
  if ($word -ne $null) { try { $word.Quit() } catch {} }
}
$result | ConvertTo-Json -Compress
""",
    "render": r"""
param([string]$InputPath, [string]$OutputDir)
$ErrorActionPreference = "Stop"
$word = $null; $doc = $null
$result = [ordered]@{open=$false; render=$false; error=$null; version=$null}
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $result.version = $word.Version
  $doc = $word.Documents.Open($InputPath, $false, $true, $false, "", "", $false, "", "", 0, $null, $false, $false, $false, $true)
  $result.open = $true
  $pdf = Join-Path $OutputDir "render.pdf"
  $doc.ExportAsFixedFormat($pdf, 17)
  $result.render = Test-Path $pdf
} catch {
  $result.error = $_.Exception.Message
} finally {
  if ($doc -ne $null) { try { $doc.Close($false) } catch {} }
  if ($word -ne $null) { try { $word.Quit() } catch {} }
}
$result | ConvertTo-Json -Compress
""",
    "save": r"""
param([string]$InputPath, [string]$OutputDir)
$ErrorActionPreference = "Stop"
$word = $null; $doc = $null
$result = [ordered]@{open=$false; save=$false; error=$null; version=$null}
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $result.version = $word.Version
  $doc = $word.Documents.Open($InputPath, $false, $true, $false, "", "", $false, "", "", 0, $null, $false, $false, $false, $true)
  $result.open = $true
  $roundtrip = Join-Path $OutputDir "roundtrip.docx"
  $doc.SaveAs2($roundtrip, 16)
  $result.save = Test-Path $roundtrip
} catch {
  $result.error = $_.Exception.Message
} finally {
  if ($doc -ne $null) { try { $doc.Close($false) } catch {} }
  if ($word -ne $null) { try { $word.Quit() } catch {} }
}
$result | ConvertTo-Json -Compress
""",
    "reopen": r"""
param([string]$InputPath)
$ErrorActionPreference = "Stop"
$word = $null; $doc = $null
$result = [ordered]@{open=$false; error=$null; version=$null}
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $result.version = $word.Version
  $doc = $word.Documents.Open($InputPath, $false, $true, $false, "", "", $false, "", "", 0, $null, $false, $false, $false, $true)
  $result.open = $true
} catch {
  $result.error = $_.Exception.Message
} finally {
  if ($doc -ne $null) { try { $doc.Close($false) } catch {} }
  if ($word -ne $null) { try { $word.Quit() } catch {} }
}
$result | ConvertTo-Json -Compress
""",
}

WORD_OPEN_SCRIPT = WORD_PHASE_SCRIPTS["open"]


def _winword_pids(proc_name: str = "WINWORD") -> set[int]:
    try:
        run = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"@(Get-Process {proc_name} -ErrorAction SilentlyContinue | ForEach-Object {{$_.Id}}) -join ','",
            ],
            capture_output=True, text=True, timeout=20,
        )
        return {int(pid) for pid in run.stdout.strip().split(",") if pid.strip().isdigit()}
    except Exception:  # noqa: BLE001
        return set()


def _kill_pids(pids: set[int]) -> None:
    if not pids:
        return
    subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"Stop-Process -Id {','.join(map(str, sorted(pids)))} -Force -ErrorAction SilentlyContinue",
        ],
        capture_output=True, timeout=20,
    )


# WPS (KWPS.Application) exposes a Word-compatible automation surface; the
# same phase scripts run against it with a different ProgID.
def _phase_script(prog_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for phase, template in WORD_PHASE_SCRIPTS.items():
        out[phase] = template.replace("Word.Application", prog_id)
    return out


WPS_PHASE_SCRIPTS = _phase_script("KWPS.Application")
WORD_PREFLIGHT_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$word = $null
$result = [ordered]@{healthy=$false; error=$null; version=$null; build=$null}
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $result.version = $word.Version
  $result.build = $word.Build
  $result.healthy = $true
} catch {
  $result.error = $_.Exception.Message
} finally {
  if ($word -ne $null) { try { $word.Quit() } catch {} }
}
$result | ConvertTo-Json -Compress
"""


def _word_preflight(prog_id: str = "Word.Application") -> dict[str, Any]:
    """Session-health preflight: a bounded COM activation probe.  A broken
    desktop session (prototype: 0x80080005 after a timeout) must not be
    retried in place."""
    probe_dir = REPO_ROOT / "qualification" / "evidence" / ".preflight"
    probe_dir.mkdir(parents=True, exist_ok=True)
    script = probe_dir / f"preflight-{Path(prog_id).stem}.ps1"
    script.write_text(WORD_PREFLIGHT_SCRIPT.replace("Word.Application", prog_id), encoding="utf-8-sig")
    started = time.monotonic()
    try:
        run = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=SESSION_PREFLIGHT_TIMEOUT,
        )
        payload: dict[str, Any] = {}
        try:
            payload = json.loads((run.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            payload = {"error": (run.stdout or run.stderr or "")[-200:]}
        healthy = bool(payload.get("healthy")) and not payload.get("error")
        return {
            "healthy": healthy,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "payload": payload,
        }
    except subprocess.TimeoutExpired:
        _kill_pids(_winword_pids())
        return {"healthy": False, "elapsed_seconds": round(time.monotonic() - started, 2), "payload": {"error": "preflight timeout"}}
    finally:
        _kill_pids(_winword_pids())


class WordRunner:
    """Phase-isolated Word COM runner with watchdog + quarantine."""

    def __init__(self, prog_id: str = "Word.Application") -> None:
        self.prog_id = prog_id
        self.scripts = (
            WPS_PHASE_SCRIPTS if prog_id != "Word.Application" else WORD_PHASE_SCRIPTS
        )
        self.phases = PHASES_WORD if prog_id == "Word.Application" else PHASES_LO
        self.proc_name = "WINWORD" if prog_id == "Word.Application" else "wps"
        self.quarantined = False
        self.quarantine_reason = ""
        self.preflight = _word_preflight(prog_id=prog_id)

    def _run_phase(self, phase: str, script_dir: Path, args: list[str], timeout: int | None = None) -> dict[str, Any]:
        script_dir.mkdir(parents=True, exist_ok=True)
        script = script_dir / f"phase-{phase}.ps1"
        script.write_text(self.scripts[phase], encoding="utf-8-sig")
        before_pids = _winword_pids(self.proc_name)
        started = time.monotonic()
        timeout = timeout or PHASE_TIMEOUT_SECONDS
        try:
            run = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(script), *args,
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )
            timed_out = False
            stdout = run.stdout or ""
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout = ""
        elapsed = round(time.monotonic() - started, 2)
        spawned = _winword_pids(self.proc_name) - before_pids
        if timed_out or spawned:
            _kill_pids(spawned)
        if timed_out:
            self.quarantined = True
            self.quarantine_reason = f"phase {phase} timed out after {timeout}s; runner quarantined"
        payload: dict[str, Any] = {}
        if not timed_out:
            try:
                payload = json.loads(stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                payload = {"error": (stdout or "no JSON result")[-200:]}
        return {
            "phase": phase,
            "elapsed_seconds": elapsed,
            "timed_out": timed_out,
            "cleanup": "killed" if (timed_out or spawned) else "none-needed",
            "payload": payload,
        }

    def run_cells(self, fixture: Path, work: Path, consumer_id: str, saved_copy: Path | None = None) -> list[dict[str, Any]]:
        out = work / consumer_id / fixture.stem
        out.mkdir(parents=True, exist_ok=True)
        cells: list[dict[str, Any]] = []

        def cell(phase: str, result: str, reason: str = "", **extra: Any) -> dict[str, Any]:
            return {
                "consumer": consumer_id,
                "fixture": fixture.name,
                "fixture_sha256": sha256(fixture),
                "phase": phase,
                "result": result,
                "reason": reason,
                **extra,
            }

        before = semantic_signature(fixture)
        if not self.preflight["healthy"]:
            reason = f"session-health preflight failed: {self.preflight['payload']}"
            for phase in self.phases:
                cells.append(cell(phase, "not-run", reason))
            return cells
        pre = prevalidate(fixture)
        if not pre["valid"]:
            reason = f"prevalidation failed before {self.prog_id}: {pre}"
            for phase in self.phases:
                cells.append(cell(phase, "fail", reason))
            return cells
        for phase in ("open", "render", "save"):
            if self.quarantined:
                cells.append(cell(phase, "not-run", f"quarantined: {self.quarantine_reason}"))
                continue
            args = [str(fixture.resolve())]
            if phase in ("render", "save"):
                args.append(str(out.resolve()))
            record = self._run_phase(phase, out, args)
            ok = not record["timed_out"] and record["payload"].get("open" if phase == "open" else phase) is True
            cells.append(
                cell(
                    phase,
                    "pass" if ok else ("fail" if not record["timed_out"] else "not-run"),
                    reason=(
                        record["payload"].get("error", "")[:200]
                        if not ok and not record["timed_out"]
                        else (record["payload"].get("error", "timed out") if record["timed_out"] else "")
                    ),
                    **{"process": record, "repair_observable": False,
                       "repair_note": "COM exposes open success/error only; no repaired signal"},
                )
            )
            if phase == "save" and ok:
                saved = out / "roundtrip.docx"
                if not saved.exists() and record["payload"].get("save"):
                    saved = Path(str(out)) / "roundtrip.docx"
        # reopen the saved copy (a fresh isolated phase)
        saved = out / "roundtrip.docx"
        if self.quarantined:
            cells.append(cell("reopen", "not-run", f"quarantined: {self.quarantine_reason}"))
        elif not saved.exists():
            cells.append(cell("reopen", "fail", reason="no saved copy to reopen"))
        else:
            record = self._run_phase("reopen", out, [str(saved.resolve())])
            ok = not record["timed_out"] and record["payload"].get("open") is True
            cells.append(
                cell(
                    "reopen",
                    "pass" if ok else ("fail" if not record["timed_out"] else "not-run"),
                    reason=record["payload"].get("error", "")[:200] if not ok and not record["timed_out"] else "",
                    **{"process": record, "repair_observable": False,
                       "repair_note": "COM exposes open success/error only; no repaired signal"},
                )
            )
        # retention
        if self.quarantined:
            cells.append(cell("retention", "not-run", f"quarantined: {self.quarantine_reason}"))
        elif saved.exists():
            after = semantic_signature(saved)
            retention = evaluate_retention(before, after, consumer_id)
            post = prevalidate(saved)
            cells.append(
                cell(
                    "retention",
                    "pass" if retention["retained"] and post["valid"] else "fail",
                    reason=f"rule={retention['rule_id']} retained={retention['retained']} diffs={retention['diffs'][:4]} post_valid={post['valid']}",
                    **{"semantic_before": before, "semantic_after": after, "retention": retention, "post_prevalidation": post},
                )
            )
        else:
            cells.append(cell("retention", "fail", reason="no saved copy for retention"))
        # repair-observation: human interactive only (Word exposes no
        # programmatic repaired signal per prototype finding)
        if self.prog_id == "Word.Application":
            cells.append(
                cell(
                    "repair-observation",
                    "not-run",
                    "human interactive observation required; Word exposes no reliable programmatic repair signal",
                )
            )
        return cells


# ---------------------------------------------------------------------------
# Calibration (seeded corruption, runs first, calibrates each adapter)
# ---------------------------------------------------------------------------


def run_calibration(work: Path) -> list[dict[str, Any]]:
    """Calibrate each runnable adapter against the four seeded corruptions.
    Calibration failures flag the adapter (calibrated=False).  Own runner
    instances: a calibration timeout never quarantines the cell runners."""
    calibration: list[dict[str, Any]] = []
    cal_dir = REPO_ROOT / "corpus" / "calibration"
    word_runner = WordRunner() if not os.environ.get("DOCX2TYPED_OFFICE_NO_WORD") else None
    wps_runner = WordRunner(prog_id="KWPS.Application") if _wps_install() else None
    for name in CALIBRATION_FIXTURES:
        bad = cal_dir / name
        entry: dict[str, Any] = {"fixture": name, "fixture_sha256": sha256(bad)}
        # LO: conversion must fail closed (no pdf, or prevalidation rejects)
        pre = prevalidate(bad)
        lo_dir = work / "calibration" / "lo" / name
        lo_dir.mkdir(parents=True, exist_ok=True)
        raw = _lo_convert(bad, lo_dir, target="pdf", phase_dir=lo_dir / "profile")
        lo_fail_closed = not raw["artifact_exists"] or not pre["valid"]
        entry["libreoffice"] = {
            "rc": raw["exit_code"],
            "artifact_exists": raw["artifact_exists"],
            "prevalidation_valid": pre["valid"],
            "fail_closed": lo_fail_closed,
        }
        # Word: open must be rejected with OpenAndRepair=false
        if word_runner is not None:
            entry["word-windows"] = _calibrate_com_runner(word_runner, work, bad, name, key="word")
        else:
            entry["word-windows"] = {"fail_closed": False, "reason": "Word calibration disabled by operator (DOCX2TYPED_OFFICE_NO_WORD)"}
        # WPS (best-effort): open must be rejected as well
        if wps_runner is not None:
            entry["wps-windows"] = _calibrate_com_runner(wps_runner, work, bad, name, key="wps")
        calibration.append(entry)
    return calibration


def _calibrate_com_runner(runner: WordRunner, work: Path, bad: Path, name: str, *, key: str = "word") -> dict[str, Any]:
    if runner.quarantined:
        return {"fail_closed": False, "reason": f"quarantined: {runner.quarantine_reason}"}
    if runner.preflight["healthy"]:
        # WPS shows a repair dialog on corrupt files (observed) which blocks
        # automation; a short calibration budget bounds that prompt.
        budget = 40 if runner.prog_id != "Word.Application" else PHASE_TIMEOUT_SECONDS
        record = runner._run_phase("open", work / "calibration" / key / name, [str(bad.resolve())], timeout=budget)
        rejected = not record["timed_out"] and record["payload"].get("open") is not True
        return {
            "fail_closed": rejected,
            "timed_out": record["timed_out"],
            "payload": record["payload"],
            "cleanup": record["cleanup"],
        }
    return {"fail_closed": False, "reason": f"session unhealthy: {runner.preflight['payload']}"}


# ---------------------------------------------------------------------------
# Evidence revision (immutable)
# ---------------------------------------------------------------------------


def next_revision() -> int:
    if not EVIDENCE_ROOT.is_dir():
        return 1
    existing = [int(p.name.removeprefix("rev-")) for p in EVIDENCE_ROOT.iterdir() if p.is_dir() and p.name.startswith("rev-")]
    return max(existing, default=0) + 1


def collect(
    *,
    fixtures: list[Path],
    consumer_filter: list[str] | None = None,
    run_calib: bool = True,
    work: Path | None = None,
) -> dict[str, Any]:
    """Run every requested adapter phase over every fixture and write the
    next immutable evidence revision."""
    work = work or (REPO_ROOT / "qualification" / "evidence" / ".work")
    work.mkdir(parents=True, exist_ok=True)
    revision = next_revision()
    selected = [name for name in CONSUMERS if consumer_filter is None or name in consumer_filter]
    host = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    consumers: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    runner: WordRunner | None = None

    def consumer_record(consumer_id: str) -> dict[str, Any]:
        spec = CONSUMERS[consumer_id]
        return {
            "id": consumer_id,
            **spec,
            "available": _consumer_available(consumer_id),
            "version_observed": {
                "word": None,
                "libreoffice": _lo_version() if consumer_id == "lo-windows" else None,
            },
        }

    for consumer_id in selected:
        consumers.append(consumer_record(consumer_id))
    calibration: list[dict[str, Any]] = []
    if run_calib:
        # seeded-corruption calibration runs FIRST and calibrates each
        # adapter.  Calibration uses its own runner instances: a calibration
        # timeout (e.g. WPS's repair dialog on corrupt files) must not
        # poison the cell run — the cell runners run their own session-health
        # preflight and per-phase watchdog/quarantine.
        calibration = run_calibration(work)
    word_runner = WordRunner() if any(CONSUMERS[c]["adapter"] == "word-windows" and _consumer_available(c) for c in selected) else None
    wps_runner = WordRunner(prog_id="KWPS.Application") if any(CONSUMERS[c]["adapter"] == "wps" and _consumer_available(c) for c in selected) else None
    for consumer_id in selected:
        spec = CONSUMERS[consumer_id]
        if not _consumer_available(consumer_id):
            # unavailable on this host (or Word collection disabled by the
            # operator): every phase is recorded not-run, honestly
            for fixture in fixtures:
                reason = (
                    "WPS not installed (non-blocking best-effort)"
                    if consumer_id == "wps-windows"
                    else (
                        "Word COM collection disabled by operator "
                        "(DOCX2TYPED_OFFICE_NO_WORD); Word save/render unobservable on this runner"
                        if consumer_id == "word-windows-m365" and os.environ.get("DOCX2TYPED_OFFICE_NO_WORD")
                        else f"consumer not available on this host: {spec.get('license_note', '')}"
                    )
                )
                for phase in spec["phases"]:
                    cells.append(
                        {
                            "consumer": consumer_id,
                            "fixture": fixture.name,
                            "fixture_sha256": sha256(fixture),
                            "phase": phase,
                            "result": "not-run",
                            "reason": reason,
                        }
                    )
            continue
        if spec["adapter"] == "word-windows":
            assert word_runner is not None
            for fixture in fixtures:
                cells.extend(word_runner.run_cells(fixture, work, consumer_id))
        elif spec["adapter"] == "wps":
            assert wps_runner is not None
            for fixture in fixtures:
                cells.extend(wps_runner.run_cells(fixture, work, consumer_id))
        elif spec["adapter"] == "libreoffice":
            for fixture in fixtures:
                cells.extend(run_lo_phases(fixture, work, consumer_id))
        else:
            for fixture in fixtures:
                reason = f"consumer not available on this host: {spec.get('license_note', '')}"
                for phase in spec["phases"]:
                    cells.append(
                        {
                            "consumer": consumer_id,
                            "fixture": fixture.name,
                            "fixture_sha256": sha256(fixture),
                            "phase": phase,
                            "result": "not-run",
                            "reason": reason,
                        }
                    )
    revision_dir = EVIDENCE_ROOT / f"rev-{revision}"
    if revision_dir.exists():
        raise RuntimeError(f"evidence revision {revision} already exists; refusing to overwrite (immutable)")
    revision_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "revision": revision,
        "generated": _now_iso(),
        "host": host,
        "consumers": consumers,
        "fixtures": [{"path": str(f), "name": f.name, "sha256": sha256(f)} for f in fixtures],
        "calibration": calibration,
        "cells": cells,
        "blocking_summary": summarize_blocking(cells, consumers),
    }
    evidence_path = revision_dir / "evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"revision": revision, "path": str(evidence_path), "cells": len(cells), "evidence": evidence}


def _consumer_available(consumer_id: str) -> bool:
    spec = CONSUMERS[consumer_id]
    if spec["adapter"] == "word-windows":
        if os.environ.get("DOCX2TYPED_OFFICE_NO_WORD"):
            return False
        return WORD_PATH.exists() and platform.system() == "Windows"
    if spec["adapter"] == "libreoffice":
        return SOFFICE_PATH.exists() and platform.system() == "Windows"
    if spec["adapter"] == "wps":
        return _wps_install() is not None
    return False


def _wps_install() -> Path | None:
    candidates = [
        Path(r"D:/programs/WPS/WPS Office/12.1.0.24034/office6/wps.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Kingsoft" / "WPS Office",
        Path(r"C:/Program Files/Kingsoft/WPS Office"),
    ]
    for candidate in candidates:
        if candidate.is_file() or candidate.exists():
            if candidate.is_dir():
                matches = sorted(candidate.glob("**/wps.exe"))
                if matches:
                    return matches[0]
            else:
                return candidate
    return None


# ---------------------------------------------------------------------------
# Blocking evaluation (fail closed)
# ---------------------------------------------------------------------------


def load_latest_evidence() -> dict[str, Any]:
    revisions = sorted(
        (p for p in EVIDENCE_ROOT.iterdir() if p.is_dir() and p.name.startswith("rev-")),
        key=lambda p: int(p.name.removeprefix("rev-")),
    )
    if not revisions:
        raise FileNotFoundError(f"no evidence revisions under {EVIDENCE_ROOT}")
    path = revisions[-1] / "evidence.json"
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_blocking(cells: list[dict[str, Any]], consumers: list[dict[str, Any]]) -> dict[str, Any]:
    """Blocking cells per issue #42: every role=blocking /
    blocking-platform-integration consumer phase must be pass; any
    not-run/not-observable/unknown/fail blocks the release gate."""
    blocking_consumers = {
        c["id"] for c in consumers
        if c.get("role") in ("blocking", "blocking-platform-integration")
    }
    blocking_cells = [cell for cell in cells if cell["consumer"] in blocking_consumers]
    not_pass = [cell for cell in blocking_cells if cell["result"] != "pass"]
    by_consumer: dict[str, dict[str, int]] = {}
    for cell in blocking_cells:
        bucket = by_consumer.setdefault(cell["consumer"], {"total": 0})
        bucket["total"] += 1
        bucket[cell["result"]] = bucket.get(cell["result"], 0) + 1
    return {
        "blocking_cells_total": len(blocking_cells),
        "blocking_cells_pass": len(blocking_cells) - len(not_pass),
        "blocking_cells_not_pass": len(not_pass),
        "by_consumer": by_consumer,
        "gate": "pass" if not not_pass else "fail",
        "blocking_not_pass_cells": [
            {
                "consumer": cell["consumer"],
                "fixture": cell["fixture"],
                "phase": cell["phase"],
                "result": cell["result"],
                "reason": cell.get("reason", "")[:160],
            }
            for cell in not_pass
        ],
    }


def missing_blocking_cells(evidence: dict[str, Any]) -> list[str]:
    """Rows the issue #42 declaration requires (blocking consumer x phase x
    fixture) that carry no explicit pass/fail verdict in the evidence.

    Absence is missing evidence, never a pass: a declared blocking consumer
    with zero recorded cells (word-ltsc-2024, word-macos-m365, lo-fresh-linux,
    lo-still-linux on this runner) or a phase recorded only as not-run
    (human repair-observation) must fail the gate with a listing.  WPS is
    best-effort and never part of the blocking matrix.
    """
    fixtures = [entry["name"] for entry in evidence.get("fixtures", [])]
    if not fixtures:
        fixtures = sorted({cell["fixture"] for cell in evidence.get("cells", [])})
    present: dict[tuple[str, str, str], str] = {
        (cell["consumer"], cell["fixture"], cell["phase"]): cell["result"]
        for cell in evidence.get("cells", [])
    }
    missing: list[str] = []
    for consumer_id in sorted(CONSUMERS):
        spec = CONSUMERS[consumer_id]
        if spec.get("role") not in BLOCKING_ROLES:
            continue
        for fixture in fixtures:
            for phase in spec["phases"]:
                result = present.get((consumer_id, fixture, phase))
                if result not in ("pass", "fail"):
                    missing.append(
                        f"{consumer_id} {fixture} {phase}: {result if result is not None else 'missing'}"
                    )
    return missing


def verify(
    evidence: dict[str, Any] | None = None,
    *,
    evidence_path: Path | None = None,
    expect_sha256: str | None = None,
) -> dict[str, Any]:
    """Fail-closed gate over the pinned evidence revision.

    ``evidence_path`` pins which revision is verified (the plan's identity
    path, not whatever happens to be latest); ``expect_sha256`` is the
    frozen identity — the file must match it or the gate errors before any
    verdict is scored.
    """
    if evidence is None:
        evidence = (
            json.loads(evidence_path.read_text(encoding="utf-8"))
            if evidence_path is not None
            else load_latest_evidence()
        )
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        raise ValueError(f"evidence schema {evidence.get('schema')!r} != {EVIDENCE_SCHEMA}")
    if expect_sha256 is not None:
        try:
            from .protocol import semantic_sha256
        except ImportError:  # direct script execution has no package context
            from protocol import semantic_sha256
        actual = semantic_sha256(evidence)
        if actual != expect_sha256:
            raise ValueError(
                f"evidence identity pin mismatch: expected {expect_sha256} got {actual}"
            )
    summary = summarize_blocking(evidence["cells"], evidence["consumers"])
    for cell in summary["blocking_not_pass_cells"]:
        print(f"BLOCKING {cell['consumer']} {cell['fixture']} {cell['phase']}: {cell['result']} — {cell['reason']}")
    missing = missing_blocking_cells(evidence)
    for row in missing:
        print(f"MISSING BLOCKING {row}")
    if missing:
        summary["gate"] = "fail"
        summary["missing_blocking_cells"] = missing
    print(
        f"gate: {summary['gate']} "
        f"({summary['blocking_cells_pass']}/{summary['blocking_cells_total']} blocking cells pass; "
        f"{len(missing)} missing rows)"
    )
    return summary


def print_matrix(evidence: dict[str, Any] | None = None) -> None:
    evidence = evidence or load_latest_evidence()
    print(f"revision {evidence['revision']} generated {evidence.get('generated')}")
    for consumer in evidence["consumers"]:
        print(f"\n== {consumer['id']} (role={consumer['role']}, available={consumer['available']}) ==")
        cells = [c for c in evidence["cells"] if c["consumer"] == consumer["id"]]
        phases = consumer["phases"]
        fixtures = sorted({c["fixture"] for c in cells})
        header = "  " + "".join(f"{p:>16s}" for p in phases)
        print(header)
        for fixture in fixtures:
            row = {c["phase"]: c["result"] for c in cells if c["fixture"] == fixture}
            print(f"{fixture:40s}" + "".join(f"{row.get(p, '-'):>16s}" for p in phases))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true", help="run adapters and write the next evidence revision")
    parser.add_argument("--verify", action="store_true", help="fail-closed gate over the latest committed revision")
    parser.add_argument("--matrix", action="store_true", help="print the committed matrix")
    parser.add_argument("--consumer", action="append", help="restrict --collect to named consumers")
    parser.add_argument("--no-calibration", action="store_true", help="skip seeded-corruption calibration in --collect")
    parser.add_argument("--fixtures", nargs="*", default=None, help="fixture paths for --collect (default: all release fixtures)")
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--evidence", type=Path, default=None, help="pinned evidence file for --verify (default: latest revision)")
    parser.add_argument("--expect-sha256", default=None, help="frozen identity; the pinned evidence file must match or --verify fails")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            summary = verify(evidence_path=args.evidence, expect_sha256=args.expect_sha256)
            return 0 if summary["gate"] == "pass" else 1
        if args.matrix:
            print_matrix()
            return 0
        if args.collect:
            if args.fixtures:
                fixture_paths = [Path(path) for path in args.fixtures]
            else:
                fixture_paths = sorted((REPO_ROOT / "corpus" / "release").glob("*.docx"))
            missing = [p for p in fixture_paths if not p.is_file()]
            if missing:
                print(f"missing fixtures: {missing}")
                return 2
            result = collect(
                fixtures=fixture_paths,
                consumer_filter=args.consumer,
                run_calib=not args.no_calibration,
                work=args.work,
            )
            print(json.dumps(
                {"revision": result["revision"], "evidence": result["path"], "cells": result["cells"]},
                ensure_ascii=False, indent=2,
            ))
            return 0
        parser.print_help()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"office evidence error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
