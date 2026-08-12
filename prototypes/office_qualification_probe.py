"""THROWAWAY PROTOTYPE: calibrate desktop Office release probes.

Run from the repository root:
  python prototypes/office_qualification_probe.py --consumer auto \
    --fixture corpus/release/review.docx \
    --fixture corpus/release/revisions.docx \
    --seed-corruption --out office-probe.json

The report separates open, render, save-roundtrip, semantic preservation, and
repair observability.  It deliberately does not call product-engine code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import unescape

WORD_PS = r'''
param([string]$InputPath, [string]$OutputDir)
$ErrorActionPreference = "Stop"
$word = $null
$doc = $null
$result = [ordered]@{open=$false; render=$false; roundtrip=$false; error=$null}
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $result.version = $word.Version
  # OpenAndRepair is argument 13 and remains false.
  $doc = $word.Documents.Open($InputPath, $false, $true, $false, "", "", $false, "", "", 0, $null, $false, $false, $false, $true)
  $result.open = $true
  $pdf = Join-Path $OutputDir "render.pdf"
  $doc.ExportAsFixedFormat($pdf, 17)
  $result.render = Test-Path $pdf
  $roundtrip = Join-Path $OutputDir "roundtrip.docx"
  $doc.SaveAs2($roundtrip, 16)
  $result.roundtrip = Test-Path $roundtrip
  $doc.Close($false)
  $doc = $word.Documents.Open($roundtrip, $false, $true, $false, "", "", $false, "", "", 0, $null, $false, $false, $false, $true)
  $result.reopen = $true
} catch {
  $result.error = $_.Exception.Message
} finally {
  if ($doc -ne $null) { try { $doc.Close($false) } catch {} }
  if ($word -ne $null) { try { $word.Quit() } catch {} }
}
$result | ConvertTo-Json -Compress
'''

WORD_OPEN_PS = r'''
param([string]$InputPath)
$ErrorActionPreference = "Stop"
$word = $null
$doc = $null
$result = [ordered]@{open=$false; error=$null}
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
'''

MAC_SCRIPT = r'''
on run argv
  set inputPath to item 1 of argv
  set outputDir to item 2 of argv
  tell application "Microsoft Word"
    set display alerts to alerts none
    open POSIX file inputPath
    set d to active document
    save as d file name (outputDir & "/roundtrip.docx") file format format document
    try
      save as d file name (outputDir & "/render.pdf") file format format PDF
      set rendered to true
    on error renderError
      set rendered to false
    end try
    close d saving no
  end tell
  return "{\"open\":true,\"roundtrip\":true,\"render\":" & rendered & "}"
end run
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def signature(path: Path) -> dict[str, Any]:
    """Small semantic signature; Word/LO roundtrips are never byte-compared."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(archive.namelist())
            xml = {
                name: archive.read(name)
                for name in names
                if name.startswith("word/") and name.endswith(".xml")
            }
    except (OSError, zipfile.BadZipFile) as exc:
        return {"valid_zip": False, "error": str(exc)}
    visible: list[str] = []
    for value in xml.values():
        visible.extend(
            unescape(match.decode("utf-8", errors="replace"))
            for match in re.findall(rb"<w:t[^>]*>(.*?)</w:t>", value, re.S)
        )
    blob = b"".join(xml.values())
    special = [
        name for name in names
        if any(token in name.lower() for token in ("comment", "people"))
    ]
    return {
        "valid_zip": True,
        "visible_text_sha256": hashlib.sha256("".join(visible).encode()).hexdigest(),
        "visible_text_chars": len("".join(visible)),
        "revisions": len(re.findall(rb"<w:(?:ins|del)(?:[ >])", blob)),
        "comments": len(re.findall(rb"<w:comment(?:[ >])", blob)),
        "comment_parts": special,
    }


def command(args: list[str], timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        run = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        return {
            "exit_code": run.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stdout": (run.stdout or "")[-2000:],
            "stderr": (run.stderr or "")[-2000:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stdout": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
            "timed_out": True,
        }


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


def libreoffice_path() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    windows = Path(r"C:/Program Files/LibreOffice/program/soffice.exe")
    return str(windows) if windows.exists() else None


def run_lo(fixture: Path, output: Path, timeout: int) -> dict[str, Any]:
    soffice = libreoffice_path()
    if not soffice:
        return {"consumer": "libreoffice", "available": False}
    output.mkdir(parents=True)
    profile = output / "profile"
    pdf_dir = output / "pdf"
    docx_dir = output / "docx"
    pdf_dir.mkdir()
    docx_dir.mkdir()
    common = [soffice, "--headless", "--norestore", "--nolockcheck", "--nofirststartwizard", f"-env:UserInstallation={file_url(profile)}"]
    render = command(common + ["--convert-to", "pdf:writer_pdf_Export", "--outdir", str(pdf_dir), str(fixture)], timeout)
    pdf = pdf_dir / f"{fixture.stem}.pdf"
    # A fresh isolated profile avoids an existing LO process swallowing flags.
    profile2 = output / "profile-roundtrip"
    common2 = [soffice, "--headless", "--norestore", "--nolockcheck", "--nofirststartwizard", f"-env:UserInstallation={file_url(profile2)}"]
    roundtrip = command(common2 + ["--convert-to", 'docx:Office Open XML Text', "--outdir", str(docx_dir), str(fixture)], timeout)
    saved = docx_dir / fixture.name
    version = command([soffice, "--version"], 20)
    before = signature(fixture)
    after = signature(saved) if saved.exists() else {"valid_zip": False, "error": "no artifact"}
    return {
        "consumer": "libreoffice",
        "available": True,
        "version": (version["stdout"] or version["stderr"]).strip(),
        "open": render["exit_code"] == 0 and pdf.exists(),
        "render": {**render, "artifact": str(pdf), "artifact_exists": pdf.exists()},
        "roundtrip": {**roundtrip, "artifact": str(saved), "artifact_exists": saved.exists()},
        "semantic_before": before,
        "semantic_after": after,
        "semantic_equal": before == after,
        "repair_observable": False,
    }


def run_lo_open(fixture: Path, output: Path, timeout: int) -> dict[str, Any]:
    soffice = libreoffice_path()
    if not soffice:
        return {"consumer": "libreoffice", "available": False, "open": False}
    output.mkdir(parents=True)
    pdf_dir = output / "pdf"
    pdf_dir.mkdir()
    raw = command([
        soffice, "--headless", "--norestore", "--nolockcheck", "--nofirststartwizard",
        f"-env:UserInstallation={file_url(output / 'profile')}",
        "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(pdf_dir), str(fixture),
    ], timeout)
    artifact = pdf_dir / f"{fixture.stem}.pdf"
    return {
        "consumer": "libreoffice",
        "available": True,
        "open": raw["exit_code"] == 0 and artifact.exists(),
        "render": {**raw, "artifact": str(artifact), "artifact_exists": artifact.exists()},
        "repair_observable": False,
    }


def new_winword_pids() -> set[int]:
    check = command([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "@(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object {$_.Id}) -join ','",
    ], 20)
    return {int(pid) for pid in check["stdout"].strip().split(",") if pid.strip().isdigit()}


def stop_pids(pids: set[int]) -> None:
    if not pids:
        return
    command([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"Stop-Process -Id {','.join(map(str, sorted(pids)))} -Force -ErrorAction SilentlyContinue",
    ], 20)


def run_word_windows(fixture: Path, output: Path, timeout: int) -> dict[str, Any]:
    if platform.system() != "Windows" or not shutil.which("powershell"):
        return {"consumer": "word-windows", "available": False}
    output.mkdir(parents=True)
    script = output / "probe.ps1"
    script.write_text(WORD_PS, encoding="utf-8-sig")
    before_pids = new_winword_pids()
    raw = command([
        "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(script), str(fixture.resolve()), str(output.resolve()),
    ], timeout)
    if raw["timed_out"]:
        stop_pids(new_winword_pids() - before_pids)
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(raw["stdout"].strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {"error": "no JSON result"}
    saved = output / "roundtrip.docx"
    before = signature(fixture)
    after = signature(saved) if saved.exists() else {"valid_zip": False, "error": "no artifact"}
    return {
        "consumer": "word-windows",
        "available": True,
        "process": raw,
        **payload,
        "roundtrip_artifact": str(saved),
        "semantic_before": before,
        "semantic_after": after,
        "semantic_equal": before == after,
        "repair_observable": False,
        "repair_note": "COM exposes open success/error, not whether Word silently repaired markup.",
    }


def run_word_windows_open(fixture: Path, output: Path, timeout: int) -> dict[str, Any]:
    if platform.system() != "Windows" or not shutil.which("powershell"):
        return {"consumer": "word-windows", "available": False, "open": False}
    output.mkdir(parents=True)
    script = output / "probe-open.ps1"
    script.write_text(WORD_OPEN_PS, encoding="utf-8-sig")
    before_pids = new_winword_pids()
    raw = command([
        "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(script), str(fixture.resolve()),
    ], timeout)
    if raw["timed_out"]:
        stop_pids(new_winword_pids() - before_pids)
    try:
        payload = json.loads(raw["stdout"].strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {"open": False, "error": "no JSON result"}
    return {
        "consumer": "word-windows",
        "available": True,
        "process": raw,
        **payload,
        "repair_observable": False,
        "repair_note": "DisplayAlerts=None cannot reveal a suppressed or silent repair.",
    }


def run_word_mac(fixture: Path, output: Path, timeout: int) -> dict[str, Any]:
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        return {"consumer": "word-macos", "available": False}
    output.mkdir(parents=True)
    script = output / "probe.applescript"
    script.write_text(MAC_SCRIPT, encoding="utf-8")
    raw = command(["osascript", str(script), str(fixture.resolve()), str(output.resolve())], timeout)
    try:
        payload = json.loads(raw["stdout"].strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {"error": "no JSON result"}
    saved = output / "roundtrip.docx"
    before = signature(fixture)
    after = signature(saved) if saved.exists() else {"valid_zip": False, "error": "no artifact"}
    return {
        "consumer": "word-macos",
        "available": True,
        "process": raw,
        **payload,
        "semantic_before": before,
        "semantic_after": after,
        "semantic_equal": before == after,
        "repair_observable": False,
    }


def corruptions(source: Path, root: Path) -> list[Path]:
    root.mkdir(parents=True)
    invalid = root / "invalid-zip.docx"
    invalid.write_bytes(b"not a zip file")
    malformed = root / "malformed-document-xml.docx"
    missing = root / "missing-document-xml.docx"
    with zipfile.ZipFile(source) as src:
        with zipfile.ZipFile(malformed, "w") as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == "word/document.xml":
                    data = data[: max(1, len(data) // 2)]
                frozen = zipfile.ZipInfo(info.filename, (1980, 1, 1, 0, 0, 0))
                dst.writestr(frozen, data)
        with zipfile.ZipFile(missing, "w") as dst:
            for info in src.infolist():
                if info.filename != "word/document.xml":
                    frozen = zipfile.ZipInfo(info.filename, (1980, 1, 1, 0, 0, 0))
                    dst.writestr(frozen, src.read(info.filename))
    return [invalid, malformed, missing]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer", choices=["auto", "libreoffice", "word-windows", "word-macos", "all"], default="auto")
    parser.add_argument("--fixture", action="append", type=Path, required=True)
    parser.add_argument("--modern-comments-fixture", type=Path)
    parser.add_argument("--seed-corruption", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--out", type=Path, default=Path("office-probe.json"))
    args = parser.parse_args()

    fixtures = [path.resolve() for path in args.fixture]
    if args.modern_comments_fixture:
        fixtures.append(args.modern_comments_fixture.resolve())
    for fixture in fixtures:
        if not fixture.is_file():
            parser.error(f"fixture not found: {fixture}")

    work = (args.work or Path(tempfile.mkdtemp(prefix="docx2typed-office-probe-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    adapters = {
        "libreoffice": run_lo,
        "word-windows": run_word_windows,
        "word-macos": run_word_mac,
    }
    if args.consumer == "all":
        selected = list(adapters)
    elif args.consumer == "auto":
        selected = ["libreoffice"]
        selected.append("word-windows" if platform.system() == "Windows" else "word-macos")
    else:
        selected = [args.consumer]

    results: list[dict[str, Any]] = []
    for fixture in fixtures:
        for name in selected:
            result = adapters[name](fixture, work / fixture.stem / name / "clean", args.timeout)
            result.update({"fixture": str(fixture), "fixture_sha256": sha256(fixture), "case": "clean"})
            results.append(result)

    calibration_adapters = {
        "libreoffice": run_lo_open,
        "word-windows": run_word_windows_open,
        "word-macos": run_word_mac,
    }
    calibration: list[dict[str, Any]] = []
    if args.seed_corruption:
        for bad in corruptions(fixtures[0], work / "seeded-corruption"):
            for name in selected:
                result = calibration_adapters[name](bad, work / "seeded" / bad.stem / name, args.timeout)
                produced = bool(result.get("open"))
                result.update({
                    "fixture": str(bad),
                    "fixture_sha256": sha256(bad),
                    "case": "seeded-corruption",
                    "fail_closed": not produced,
                })
                calibration.append(result)

    report = {
        "schema": "docx2typed-office-probe-prototype-1",
        "prototype": True,
        "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "work": str(work),
        "modern_comments_fixture_supplied": bool(args.modern_comments_fixture),
        "results": results,
        "seeded_corruption": calibration,
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(args.out.resolve()),
        "clean": [{"consumer": r["consumer"], "fixture": Path(r["fixture"]).name, "open": r.get("open"), "semantic_equal": r.get("semantic_equal")} for r in results],
        "calibration": [{"consumer": r["consumer"], "fixture": Path(r["fixture"]).name, "fail_closed": r["fail_closed"]} for r in calibration],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
