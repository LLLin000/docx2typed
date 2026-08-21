# rust_noop_gate.ps1 — issue #55 differential + S-profile resource gate for
# the Rust tracer (rust-side reproduction of qualification/plan.json check
# "noop-bytes").
#
# The frozen plan's noop-bytes case runs `python -m scripts extract/build`
# through the Python Reference. scripts/qualify.py is not modified (its
# adapter wiring stays Python-side), so this script reproduces the SAME
# frozen case against the Rust binary through its public CLI seam and
# compares the output to the recorded bundle identity — per acceptance #5
# ("provide a rust-side reproduction script ... state clearly which").
#
# Gates asserted:
#   - extract rc=0, build rc=0, verify rc=0 (frozen plan expects)
#   - output whole-file SHA-256 == frozen record (plan identities.fixture.
#     fixtures.plain.docx, also the byte-identical Python no-op build output
#     verified on this host)
#   - per-part SHA-256 identity vs source (frozen compare kind: noop_bytes)
#   - copy-if-unchanged: output bytes == workdir/_template.docx bytes
#   - S profile wall budgets: complete_chain_s <= 35, commit_build_s <= 10,
#     independent_verify_s <= 15 (qualification/resource_profiles.json)
#   - S profile RSS formula: peak RSS <= min(1.5 GiB,
#     12 * uncompressed_editable_xml + 256 MiB)
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File qualification/rust_noop_gate.ps1
#        [-Bin ..\target\release\docx2typed.exe] [-Fixture ..\corpus\release\plain.docx]

param(
    [string]$Bin = "target\release\docx2typed.exe",
    [string]$Fixture = "corpus\release\plain.docx"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root $Bin
$fixture = Join-Path $root $Fixture
if (-not (Test-Path $bin)) { throw "binary not found: $bin" }
if (-not (Test-Path $fixture)) { throw "fixture not found: $fixture" }

$expectedSha256 = "4323e37b7ac7e9dbce7b4923d14529bda821f0d66f0dce7005cf9299bf8d9c39"
$scratch = Join-Path $env:TEMP ("rust-noop-" + [guid]::NewGuid().ToString("N"))
$workdir = Join-Path $scratch "wd"
$output = Join-Path $scratch "out.docx"
New-Item -ItemType Directory -Path $scratch -Force | Out-Null

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-Sha256([string]$path) {
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
}

function Get-ZipPartHashes([string]$path) {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($path)
    try {
        $map = @{}
        $sha = [System.Security.Cryptography.SHA256]::Create()
        foreach ($entry in $zip.Entries) {
            $stream = $entry.Open()
            try {
                $ms = New-Object System.IO.MemoryStream
                $stream.CopyTo($ms)
                $hash = ([BitConverter]::ToString($sha.ComputeHash($ms.ToArray()))).Replace("-", "").ToLower()
                $map[$entry.FullName] = $hash
            } finally { $stream.Dispose() }
        }
        return $map
    } finally { $zip.Dispose() }
}

function Get-ZipUncompressedXmlBytes([string]$path) {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($path)
    try {
        $total = [long]0
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -match "^word/.*\.xml$") { $total += $entry.Length }
        }
        return $total
    } finally { $zip.Dispose() }
}

function New-RustProcess([string]$file, [string[]]$arguments) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $file
    $psi.Arguments = ($arguments | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    return $proc
}

function Invoke-Measured([string]$file, [string[]]$arguments) {
    $proc = New-RustProcess $file $arguments
    $started = [System.Diagnostics.Stopwatch]::StartNew()
    [void]$proc.Start()
    $proc.WaitForExit()
    return @{ rc = $proc.ExitCode; seconds = $started.Elapsed.TotalSeconds }
}

# Peak RSS sampling wrapper: samples the working set during the run.
function Invoke-MeasuredRss([string]$file, [string[]]$arguments, [ref]$peakBytes) {
    $proc = New-RustProcess $file $arguments
    $started = [System.Diagnostics.Stopwatch]::StartNew()
    [void]$proc.Start()
    $peak = [long]0
    while (-not $proc.HasExited) {
        try {
            $ws = $proc.WorkingSet64
            if ($ws -gt $peak) { $peak = $ws }
        } catch { }
        Start-Sleep -Milliseconds 10
    }
    $proc.WaitForExit()
    $peakBytes.Value = $peak
    return @{ rc = $proc.ExitCode; seconds = $started.Elapsed.TotalSeconds }
}

$extractPeakBytes = [long]0
$buildPeakBytes = [long]0

# --- extract ---------------------------------------------------------------
$extract = Invoke-MeasuredRss $bin @("extract", "--json", $fixture, "-o", $workdir) ([ref]$extractPeakBytes)
$extractRun = & $bin extract --json $fixture -o $workdir 2>&1 | Out-String
$extractEnvelope = $extractRun | ConvertFrom-Json

# --- build -----------------------------------------------------------------
$build = Invoke-MeasuredRss $bin @("build", "--json", $workdir, "-o", $output) ([ref]$buildPeakBytes)
$buildRun = & $bin build --json $workdir -o $output 2>&1 | Out-String
$buildEnvelope = $buildRun | ConvertFrom-Json
$buildPeak = [math]::Round($peak / 1MB, 1)

# --- verify ----------------------------------------------------------------
$verify = Invoke-Measured $bin @("verify", "--json", $workdir, $output)
$verifyRun = & $bin verify --json $workdir $output 2>&1 | Out-String
$verifyEnvelope = $verifyRun | ConvertFrom-Json

# --- evidence --------------------------------------------------------------
$sourceSha256 = Get-Sha256 $fixture
$outputSha256 = Get-Sha256 $output
$templateSha256 = Get-Sha256 (Join-Path $workdir "_template.docx")
$outputParts = Get-ZipPartHashes $output
$sourceParts = Get-ZipPartHashes $fixture
$partsEqual = -not (Compare-Object $outputParts $sourceParts)
$copyProof = $outputSha256 -eq $templateSha256

$xmlBytes = Get-ZipUncompressedXmlBytes $fixture
$rssBudgetMib = [math]::Min(1536, [math]::Floor(12 * ($xmlBytes / 1MB) + 256))
$peakRssMib = [math]::Round([math]::Max($extractPeakBytes, $buildPeakBytes) / 1MB, 1)
$rssPass = $peakRssMib -le $rssBudgetMib

$chainSeconds = $extract.seconds + $build.seconds + $verify.seconds
$checks = @(
    @{ name = "extract rc=0";            pass = ($extract.rc -eq 0 -and $extractEnvelope.outcome -eq "success") },
    @{ name = "build rc=0";              pass = ($build.rc -eq 0 -and $buildEnvelope.outcome -eq "success") },
    @{ name = "verify rc=0";             pass = ($verify.rc -eq 0 -and $verifyEnvelope.outcome -eq "success") },
    @{ name = "output sha256 == frozen record"; pass = ($outputSha256 -eq $expectedSha256) },
    @{ name = "per-part identity vs source";    pass = $partsEqual },
    @{ name = "copy-if-unchanged (output == template bytes)"; pass = $copyProof },
    @{ name = "S complete_chain_s <= 35"; pass = ($chainSeconds -le 35) },
    @{ name = "S commit_build_s <= 10";  pass = ($build.seconds -le 10) },
    @{ name = "S independent_verify_s <= 15"; pass = ($verify.seconds -le 15) },
    @{ name = "S peak RSS <= $rssBudgetMib MiB"; pass = $rssPass }
)
$failures = @($checks | Where-Object { -not $_.pass })
$verdict = if ($failures.Count -eq 0) { "pass" } else { "fail" }

$report = [ordered]@{
    check = "noop-bytes"
    engine = "rust"
    verdict = $verdict
    source_sha256 = $sourceSha256
    output_sha256 = $outputSha256
    expected_sha256 = $expectedSha256
    parts_identical = $partsEqual
    copy_if_unchanged = $copyProof
    wall = [ordered]@{
        extract_s = [math]::Round($extract.seconds, 3)
        build_s = [math]::Round($build.seconds, 3)
        verify_s = [math]::Round($verify.seconds, 3)
        chain_s = [math]::Round($chainSeconds, 3)
        budget_chain_s = 35
        budget_build_s = 10
        budget_verify_s = 15
    }
    rss = [ordered]@{
        peak_mib = $peakRssMib
        budget_mib = $rssBudgetMib
        formula = "min(1536 MiB, 12 * uncompressed_editable_xml + 256 MiB)"
        uncompressed_editable_xml_bytes = $xmlBytes
    }
    checks = $checks
}
$report | ConvertTo-Json -Depth 6
if ($failures.Count -gt 0) {
    Write-Host "FAILED gates: $($failures.name -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "ALL GATES PASS" -ForegroundColor Green
Remove-Item -Recurse -Force $scratch
exit 0
