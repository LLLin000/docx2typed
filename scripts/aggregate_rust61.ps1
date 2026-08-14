# aggregate_rust61.ps1 - issue #61 consolidated differential qualification:
# aggregates the six Rust gates' evidence (noop / migrate / store / prose /
# govern=tracer59 / review60) plus the recovery matrix, S/L/X resource
# numbers, and the Office matrix into one qualification bundle
# (qualification/evidence/rust_tracer61_evidence.json).
#
# The six gates must be run first against the RELEASE binary (each writes its
# own evidence JSON). The noop gate prints its report to stdout, so this
# script re-runs it and persists the report into the evidence directory.
# The release_ready verdict is honest and fails closed while the blocking
# Office matrix cells are not-run (no Word/COM host), exactly like the
# Python qualification.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/aggregate_rust61.ps1
#        [-Bin ..\target\release\docx2typed.exe]

param(
    [string]$Bin = "target\release\docx2typed.exe",
    [string]$EvidenceDir = "qualification\evidence",
    [string]$Out = "qualification\evidence\rust_tracer61_evidence.json"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$binRel = if ([IO.Path]::IsPathRooted($Bin)) {
    if ($Bin.StartsWith($root)) { $Bin.Substring($root.Length).TrimStart('\', '/') }
    else { throw "binary outside repo root: $Bin" }
} else { $Bin }
$bin = Join-Path $root $binRel
$evidenceDir = Join-Path $root $EvidenceDir
$outPath = Join-Path $root $Out
if (-not (Test-Path $bin)) { throw "release binary not found: $bin" }
if (-not (Test-Path $evidenceDir)) { throw "evidence dir not found: $evidenceDir" }

function Read-Json([string]$path) {
    return [System.IO.File]::ReadAllText($path) | ConvertFrom-Json
}

function Get-Sha256([string]$path) {
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
}

# ---- 1. noop gate (prints its report; persist it) ------------------------
$noopScript = Join-Path $root "qualification\rust_noop_gate.ps1"
$noopOut = & powershell -NoProfile -ExecutionPolicy Bypass -File $noopScript -Bin $binRel 2>&1 | Out-String
$jsonStart = $noopOut.IndexOf("{")
$jsonEnd = $noopOut.LastIndexOf("}")
if ($jsonStart -lt 0 -or $jsonEnd -lt 0) {
    throw "noop gate produced no JSON report: $noopOut"
}
$noopJson = $noopOut.Substring($jsonStart, $jsonEnd - $jsonStart + 1) | ConvertFrom-Json
$noopReportPath = Join-Path $evidenceDir "rust-noop-gate-report.json"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($noopReportPath, ($noopJson | ConvertTo-Json -Depth 8), $utf8)

# ---- 2. read the five persisted gate evidence files ----------------------
$gateFiles = @{
    noop = "rust-noop-gate-report.json"
    migrate = "rust_migrate_evidence.json"
    store = "rust_store_evidence.json"
    prose = "rust_prose_evidence.json"
    govern = "rust_tracer59_evidence.json"
    review60 = "rust_tracer60_evidence.json"
}
$gates = @()
foreach ($name in @("noop", "migrate", "store", "prose", "govern", "review60")) {
    $path = Join-Path $evidenceDir $gateFiles[$name]
    if (-not (Test-Path $path)) { throw "gate evidence missing: $path (run the gate first)" }
    $evidence = Read-Json $path
    $checks = @($evidence.checks)
    $passCount = @($checks | Where-Object { $_.pass }).Count
    $gates += [ordered]@{
        gate = $name
        evidence_file = $gateFiles[$name]
        checks_pass = $passCount
        checks_total = $checks.Count
        verdict = if ($passCount -eq $checks.Count) { "pass" } else { "fail" }
    }
}

# ---- 3. recovery matrix summary (from the store gate evidence) -----------
$storeEvidence = Read-Json (Join-Path $evidenceDir $gateFiles["store"])
$recoveryMatrix = [ordered]@{
    cuts = @($storeEvidence.fault_cuts)
    cuts_total = @($storeEvidence.fault_cuts).Count
    cuts_parity_matched = @($storeEvidence.fault_cuts | Where-Object { $_.match }).Count
    cuts_recovery_clean = @($storeEvidence.fault_cuts | Where-Object { $_.recovery_clean }).Count
    real_process_kill = $storeEvidence.real_process_kill
    exactly_once = $storeEvidence.exactly_once
    corruption = $storeEvidence.corruption
    enospc = $storeEvidence.enospc
}

# ---- 4. S/L/X resource numbers -------------------------------------------
$profilesPath = Join-Path $root "qualification\resource_profiles.json"
$profiles = Read-Json $profilesPath
$noopWall = $noopJson.wall
$noopRss = $noopJson.rss
$resourceNumbers = [ordered]@{
    profiles = $profiles.profiles
    wall_budgets = $profiles.wall_budgets
    rss_formulas = $profiles.rss_formulas
    measured_S = [ordered]@{
        noop_chain_s = $noopWall.chain_s
        noop_budget_chain_s = $noopWall.budget_chain_s
        noop_peak_rss_mib = $noopRss.peak_mib
        noop_rss_budget_mib = $noopRss.budget_mib
        noop_budget_chain_s_pass = ($noopWall.chain_s -le $noopWall.budget_chain_s)
        prose_fixture_chain_s = @($(Read-Json (Join-Path $evidenceDir $gateFiles["prose"])).fixtures | ForEach-Object { $_.rust_chain_s })
    }
}

# ---- 5. Office matrix (blocking cells not-run, no host) ------------------
$officePath = Join-Path $root "qualification\evidence\rev-1\evidence.json"
$officeCellsNotRun = 0
$officeCellsFail = 0
$officeCellsTotal = 0
$officeBlockingSummary = $null
if (Test-Path $officePath) {
    $office = Read-Json $officePath
    if ($office.blocking_summary) {
        $officeBlockingSummary = $office.blocking_summary
        $officeCellsTotal = $officeBlockingSummary.blocking_cells_total
    }
    if ($office.cells) {
        $officeCellsNotRun = @($office.cells | Where-Object { $_.result -eq "not-run" }).Count
        $officeCellsFail = @($office.cells | Where-Object { $_.result -eq "fail" }).Count
    }
}
$officeMatrix = [ordered]@{
    status = "not-run-no-host"
    evidence = "qualification/evidence/rev-1/evidence.json"
    reason = "Word/Office COM is not run on this host (no Word/Office host available); the frozen Office evidence cells stay honestly not-run, exactly like the Python #52 qualification"
    cells_total = $officeCellsTotal
    cells_not_run = $officeCellsNotRun
    cells_fail = $officeCellsFail
    blocking_summary = $officeBlockingSummary
}

# ---- 6. verdict (fail closed) --------------------------------------------
$gatesFail = @($gates | Where-Object { $_.verdict -ne "pass" })
$releaseReady = ($gatesFail.Count -eq 0) -and ($officeCellsNotRun -eq 0) -and ($officeCellsFail -eq 0)
$versionJson = & $bin --version --json | Out-String | ConvertFrom-Json

$report = [ordered]@{
    schema = "docx2typed-rust-qualification-61-1"
    generated = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    host = $env:COMPUTERNAME
    binary = [ordered]@{
        path = $bin
        sha256 = Get-Sha256 $bin
        version = $versionJson.version
        build_commit = $versionJson.build_commit
        embedded_assets = $versionJson.embedded_assets
    }
    gates = $gates
    gates_all_pass = ($gatesFail.Count -eq 0)
    recovery_matrix = $recoveryMatrix
    resource_numbers = $resourceNumbers
    office_matrix = $officeMatrix
    release_ready = $releaseReady
    verdict = if ($releaseReady) { "release-ready" } else { "qualified-candidate-not-release-ready (Office blocking cells not-run on this host: no Word/Office host)" }
}
$reportJson = $report | ConvertTo-Json -Depth 12
[System.IO.File]::WriteAllText($outPath, $reportJson, $utf8)
$reportJson
Write-Host "consolidated qualification written: $outPath" -ForegroundColor Green
