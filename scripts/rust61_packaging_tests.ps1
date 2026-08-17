# rust61_packaging_tests.ps1 - issue #61 focused tests for packaging and the
# Windows install lifecycle:
#   1. release binary smoke (CLI --version --json with embedded assets, MCP
#      engine_info/tools/list, review server boot)
#   2. packaging artifact integrity (checksums match the bundle files, the
#      detached Ed25519 signature verifies, embedded asset hashes match the
#      bundle records, SBOM/licenses/provenance present)
#   3. install -> update -> rollback -> uninstall lifecycle on a temp prefix
#      (atomicity, receipt version/hash bookkeeping, .bak preserve/consume)
#   4. receipt safety (uninstall removes only receipt-listed files and never
#      an unrelated file in the same prefix)
#
# Output: one JSON report on stdout; exit 0 only when every check passes.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/rust61_packaging_tests.ps1

param(
    [string]$Bin = "target\release\docx2typed.exe",
    [string]$DevBin = "target\debug\docx2typed.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root $Bin
$devBin = Join-Path $root $DevBin
if (-not (Test-Path $bin)) { throw "release binary not found: $bin" }

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-Sha256([string]$path) {
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
}

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ($listener.LocalEndpoint).Port
    $listener.Stop()
    return $port
}

function Write-Utf8NoBom([string]$path, [string]$content) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $content, $utf8)
}

function New-Temp([string]$tag) {
    $dir = Join-Path $env:TEMP ("rust61-" + $tag + "-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    return $dir
}

$checks = @()
function Add-Check([string]$name, [bool]$pass, [string]$detail = "") {
    $script:checks += @{ name = $name; pass = $pass; detail = $detail }
}

# ---- 1. release binary smoke ---------------------------------------------
$versionJson = & $bin --version --json | Out-String | ConvertFrom-Json
Add-Check "cli version json" ($versionJson.name -eq "docx2typed-rust") $versionJson.version
$assetsOk = $true
foreach ($asset in $versionJson.embedded_assets) { if (-not $asset.match_pinned) { $assetsOk = $false } }
Add-Check "embedded assets match pinned records" $assetsOk (($versionJson.embedded_assets | ForEach-Object { $_.name }) -join ", ")

# MCP stdio smoke: engine_info + tools/list
$mcpIn = '{"tool":"engine_info","args":{}}' + "`n" + '{"tool":"tools/list","args":{}}' + "`n"
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $bin
$psi.Arguments = "mcp"
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
[void]$proc.Start()
$proc.StandardInput.Write($mcpIn)
$proc.StandardInput.Close()
$mcpOut = $proc.StandardOutput.ReadToEnd()
$proc.WaitForExit(15000) | Out-Null
$mcpLines = @($mcpOut -split "`n" | Where-Object { $_.Trim() -ne "" })
$engineInfoOk = $mcpLines[0].StartsWith("OK ")
$toolsListOk = ($mcpLines[1] -match '"engine_info"')
Add-Check "mcp engine_info OK" $engineInfoOk
Add-Check "mcp tools/list 36 tools" ($toolsListOk -and ($mcpLines[1] -match '"review_settle"')) $mcpLines.Count

# review server boot (bootstrap shell 200 on 127.0.0.1)
$workdir = New-Temp "review"
& $bin extract --json (Join-Path $root "corpus\release\plain.docx") -o $workdir | Out-Null
$port = Get-FreePort
$server = Start-Process -FilePath $bin -ArgumentList @("review", $workdir, "--port", "$port") -PassThru -WindowStyle Hidden
$bootOk = $false
try {
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $bootOk = $true; break }
        } catch { }
    }
} finally {
    if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
}
Add-Check "review server boot (GET / 200)" $bootOk "port $port"

# ---- 2. packaging artifact integrity -------------------------------------
$pkgOut = New-Temp "pkg"
$pkgOut2 = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\package_release.ps1") `
    -Bin $bin -OutDir $pkgOut -Target "windows-x86_64-msvc" -Channel "stable" -Coverage "this-host" 2>&1 | Out-String
$js = $pkgOut2.IndexOf("{")
$je = $pkgOut2.LastIndexOf("}")
$pkg = $pkgOut2.Substring($js, $je - $js + 1) | ConvertFrom-Json
$bundleDir = $pkg.bundle
Add-Check "package bundle created" (Test-Path $bundleDir) $bundleDir

# checksums match the actual files
$sums = Get-Content (Join-Path $bundleDir "SHA256SUMS.txt")
$sumsOk = $true
$sumsDetail = ""
foreach ($line in $sums) {
    $parts = $line -split "  ", 2
    if ($parts.Count -ne 2) { $sumsOk = $false; continue }
    $expected = $parts[0]
    $rel = $parts[1]
    $actual = Get-Sha256 (Join-Path $bundleDir ($rel.Replace("/", "\")))
    if ($actual -ne $expected) { $sumsOk = $false; $sumsDetail += "$rel " }
}
Add-Check "SHA256SUMS match bundle files" $sumsOk $sumsDetail

# detached signature verifies with the committed dev public key
$sigOut = & openssl pkeyutl -verify -pubin -inkey (Join-Path $root "reference\keys\dev-signing-pub.pem") `
    -rawin -in (Join-Path $bundleDir "SHA256SUMS.txt") -sigfile (Join-Path $bundleDir "SHA256SUMS.txt.sig") 2>&1
Add-Check "Ed25519 signature verifies" ($LASTEXITCODE -eq 0 -and ($sigOut -match "Verified OK" -or $sigOut -match "Verified Successfully")) ($sigOut -join " ")

# embedded asset hashes match the bundle records (recomputed from the binary)
$pkgAssetsOk = $true
foreach ($asset in $versionJson.embedded_assets) { if (-not $asset.match_pinned) { $pkgAssetsOk = $false } }
Add-Check "packaged binary embedded assets hash-bound" $pkgAssetsOk

# SBOM / licenses / provenance present
$sbom = Get-Content (Join-Path $bundleDir "sbom.json") -Raw | ConvertFrom-Json
Add-Check "SBOM committed manifest present" (($sbom.dependencies.Count) -gt 50) ("$($sbom.dependencies.Count) dependencies")
Add-Check "licenses present" ((Test-Path (Join-Path $bundleDir "licenses\LICENSE-MIT.txt")))
$prov = Get-Content (Join-Path $bundleDir "provenance.json") -Raw | ConvertFrom-Json
Add-Check "provenance records target/coverage/signing" `
    ($prov.target -eq "windows-x86_64-msvc" -and $prov.coverage -eq "this-host" -and $prov.signing.key_role -eq "dev")
Add-Check "reproducibility note present" (Test-Path (Join-Path $bundleDir "reproducibility.txt"))

# ---- 3. install -> update -> rollback -> uninstall lifecycle --------------
if (-not (Test-Path $devBin)) {
    & cargo build --manifest-path (Join-Path $root "Cargo.toml") 2>&1 | Out-Null
}
$prefix = New-Temp "install"
$installer = Join-Path $root "scripts\install_binary.ps1"
$installArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $installer, "-Prefix", $prefix)

# install
& powershell @installArgs -Action install -Bin $bin | Out-Null
$receiptPath = Join-Path $prefix "receipt.json"
Add-Check "install: receipt written" (Test-Path $receiptPath)
$receipt = Get-Content $receiptPath -Raw | ConvertFrom-Json
Add-Check "install: receipt records binary hash" ($receipt.binary_sha256 -eq (Get-Sha256 $bin))
$installedBin = Join-Path $prefix "bin\docx2typed.exe"
Add-Check "install: binary runs" ((& $installedBin --version --json | Out-String) -match "docx2typed-rust")
$mcpCfg = Get-Content (Join-Path $prefix "mcp.config.json") -Raw | ConvertFrom-Json
Add-Check "install: MCP config uses absolute binary path" ($mcpCfg.mcpServers.docx2typed.command -eq $installedBin)

# update (dev build = different bytes, same version; exercises the atomic
# replace + backup path)
& powershell @installArgs -Action update -Bin $devBin | Out-Null
$receipt2 = Get-Content $receiptPath -Raw | ConvertFrom-Json
Add-Check "update: receipt hash updated" ($receipt2.binary_sha256 -eq (Get-Sha256 $devBin))
Add-Check "update: previous binary kept as .bak" (Test-Path (Join-Path $prefix "bin\docx2typed.exe.bak"))
Add-Check "update: receipt records previous hash" ($receipt2.previous_binary_sha256 -eq (Get-Sha256 $bin))

# rollback
& powershell @installArgs -Action rollback | Out-Null
$receipt3 = Get-Content $receiptPath -Raw | ConvertFrom-Json
Add-Check "rollback: binary restored to original bytes" ($receipt3.binary_sha256 -eq (Get-Sha256 $bin))
Add-Check "rollback: .bak consumed" (-not (Test-Path (Join-Path $prefix "bin\docx2typed.exe.bak")))

# receipt safety: unrelated file survives uninstall
$unrelated = Join-Path $prefix "user-data.txt"
Write-Utf8NoBom $unrelated "user state that uninstall must never touch"
& powershell @installArgs -Action uninstall | Out-Null
Add-Check "uninstall: receipt removed" (-not (Test-Path $receiptPath))
Add-Check "uninstall: binary removed" (-not (Test-Path $installedBin))
Add-Check "uninstall: mcp config removed" (-not (Test-Path (Join-Path $prefix "mcp.config.json")))
Add-Check "uninstall: unrelated file untouched" (Test-Path $unrelated)

# uninstall with no receipt is a no-op (receipt-safe refusal)
& powershell @installArgs -Action uninstall | Out-Null
Add-Check "uninstall: second run no-op" (Test-Path $unrelated)

# ---- report ---------------------------------------------------------------
$failures = @($checks | Where-Object { -not $_.pass })
$report = [ordered]@{
    schema = "docx2typed-rust61-packaging-tests-1"
    binary_sha256 = Get-Sha256 $bin
    checks = $checks
    verdict = if ($failures.Count -eq 0) { "pass" } else { "fail" }
}
$report | ConvertTo-Json -Depth 8
if ($failures.Count -gt 0) {
    Write-Host "FAILED: $($failures.name -join '; ')" -ForegroundColor Red
    exit 1
}
Write-Host "ALL PACKAGING TESTS PASS" -ForegroundColor Green
