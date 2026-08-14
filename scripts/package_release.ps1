# package_release.ps1 - issue #61 release packaging pipeline for the Rust
# binary: one self-contained release bundle per Tier-1 target with
#   - the release binary (embedded assets; no repo checkout needed)
#   - SHA-256 checksums over every bundle file
#   - a detached Ed25519 signature over the checksums (openssl CLI, operator-
#     key policy mirrored from issue #54: dev key from the local keystore is
#     clearly marked, operator key via DOCX2TYPED_RELEASE_KEY wins, an
#     unregistered key is refused - a signature is never fabricated)
#   - SBOM (committed manifest packaging/sbom.json; cargo-cyclonedx is
#     unavailable on this host, the committed crate+license list is used)
#   - licenses (workspace MIT text + per-crate license list in the SBOM)
#   - provenance manifest (build env, commit, toolchain, asset hashes)
#   - reproducibility note (pinned Cargo.lock + stable toolchain command)
#
# Target coverage is declared explicitly in the provenance manifest:
#   this-host       artifact produced and verified on this host
#   ci-only         artifact produced by CI (no local host verification)
#   not-run-no-host no artifact (no host/toolchain available)
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/package_release.ps1
#        [-Bin ..\target\release\docx2typed.exe]
#        [-OutDir ..\dist\release]
#        [-Target windows-x86_64-msvc]
#        [-Channel stable]
#        [-Coverage this-host]

param(
    [string]$Bin = "target\release\docx2typed.exe",
    [string]$OutDir = "dist\release",
    [string]$Target = "windows-x86_64-msvc",
    [string]$Channel = "stable",
    [string]$Coverage = "this-host"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin = if ([IO.Path]::IsPathRooted($Bin)) { $Bin } else { Join-Path $root $Bin }
if (-not (Test-Path $bin)) { throw "release binary not found: $bin" }
$outRoot = if ([IO.Path]::IsPathRooted($OutDir)) { $OutDir } else { Join-Path $root $OutDir }

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-Sha256([string]$path) {
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
}

function Write-Utf8NoBom([string]$path, [string]$content) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $content, $utf8)
}

function Canonical-Json([hashtable]$value) {
    # sorted keys, compact separators (python json.dumps sort_keys=True
    # separators=(",",":")) for stable signatures
    $sorted = [ordered]@{}
    foreach ($key in ($value.Keys | Sort-Object)) { $sorted[$key] = $value[$key] }
    return $sorted | ConvertTo-Json -Depth 20 -Compress
}

# ---- version / embedded asset identities --------------------------------
$versionJson = & $bin --version --json | Out-String
$version = $versionJson | ConvertFrom-Json
$embedded = @{}
foreach ($asset in $version.embedded_assets) {
    $embedded[$asset.name] = @{
        raw_sha256 = $asset.raw_sha256
        semantic_sha256 = $asset.semantic_sha256
        pinned_sha256 = $asset.pinned_sha256
        match_pinned = $asset.match_pinned
    }
}
$embeddedFail = @($embedded.Values | Where-Object { -not $_.match_pinned })
if ($embeddedFail.Count -gt 0) {
    throw "embedded asset hash mismatch in $bin; refusing to package"
}

# ---- bundle layout -------------------------------------------------------
$bundleDir = Join-Path $outRoot ("docx2typed-" + $Target + "-" + $Channel)
$licenseDir = Join-Path $bundleDir "licenses"
New-Item -ItemType Directory -Path $licenseDir -Force | Out-Null

$binaryName = [IO.Path]::GetFileName($bin)
Copy-Item -Path $bin -Destination (Join-Path $bundleDir $binaryName) -Force

# licenses: workspace MIT text (short form) + SBOM per-crate list
Write-Utf8NoBom (Join-Path $licenseDir "LICENSE-MIT.txt") @"
MIT License

Copyright (c) 2026 docx2typed contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"@
Copy-Item -Path (Join-Path $root "packaging\sbom.json") -Destination (Join-Path $bundleDir "sbom.json") -Force

# ---- provenance ----------------------------------------------------------
$rustcVersion = (& rustc --version 2>$null | Out-String).Trim()
$cargoVersion = (& cargo --version 2>$null | Out-String).Trim()
$commit = (& git -C $root rev-parse HEAD 2>$null | Out-String).Trim()
$commitTime = (& git -C $root log -1 --format=%ci 2>$null | Out-String).Trim()
$osInfo = [System.Environment]::OSVersion.VersionString
$binSha = Get-Sha256 $bin

# signing key policy (issue #54): operator key wins, dev key fallback,
# unregistered key refused
$keystoreDir = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".docx2typed\keys"
$devKey = Join-Path $keystoreDir "dev-signing.key"
$releaseKey = Join-Path $keystoreDir "release-signing.key"
$envKey = $env:DOCX2TYPED_RELEASE_KEY
$keyPath = $null; $keyRole = $null
if ($envKey -and (Test-Path $envKey)) { $keyPath = $envKey; $keyRole = "operator-env" }
elseif (Test-Path $releaseKey) { $keyPath = $releaseKey; $keyRole = "operator-keystore" }
elseif (Test-Path $devKey) { $keyPath = $devKey; $keyRole = "dev" }
if (-not $keyPath) {
    throw "no signing key available: set DOCX2TYPED_RELEASE_KEY, install the operator key in $releaseKey, or provision the dev key (reference/keys/README.md). A signature is never synthesized."
}

$pubKeyPath = Join-Path $root "reference\keys\dev-signing-pub.pem"
if ($keyRole -ne "dev" -and (Test-Path (Join-Path $root "reference\keys\release-signing-pub.pem"))) {
    $pubKeyPath = Join-Path $root "reference\keys\release-signing-pub.pem"
}
if (-not (Test-Path $pubKeyPath)) { throw "committed public key missing: $pubKeyPath" }

$provenance = [ordered]@{
    schema = "docx2typed-release-provenance-1"
    generated = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    target = $Target
    channel = $Channel
    coverage = $Coverage
    binary = [ordered]@{
        file = $binaryName
        sha256 = $binSha
        version = $version.version
        build_commit = $version.build_commit
        contracts = $version.contracts
        embedded_assets = $embedded
    }
    build = [ordered]@{
        commit = $commit
        commit_time = $commitTime
        rustc = $rustcVersion
        cargo = $cargoVersion
        os = $osInfo
        host = $env:COMPUTERNAME
        reproducibility = "cargo build --release --locked with the pinned Cargo.lock and the stable toolchain recorded above on the same target produces a byte-identical binary; timestamps/embedding are constant (no build-time date is embedded)"
    }
    signing = [ordered]@{
        algorithm = "Ed25519 (RFC 8032)"
        key_role = $keyRole
        key_path = $keyPath
        public_key = $pubKeyPath
        signed_payload = "SHA256SUMS.txt (canonical UTF-8, LF line endings)"
        verification = "openssl pkeyutl -verify -pubin -inkey <pub.pem> -rawin -in SHA256SUMS.txt -sigfile SHA256SUMS.txt.sig"
        note = "DEV key signature is a reproducibility artifact, not a public release; operator key required for release (reference/keys/README.md)"
    }
}
Write-Utf8NoBom (Join-Path $bundleDir "provenance.json") (Canonical-Json $provenance)

Write-Utf8NoBom (Join-Path $bundleDir "reproducibility.txt") @"
Reproducibility note (issue #61)

1. Cargo.lock is pinned in-tree; build with the recorded stable toolchain.
2. Reproduce: cargo build --release --locked
   (workspace root; the release binary embeds the pinned schema bundle,
   capability manifest, and Unicode vertical catalog via include_str!).
3. The binary embeds no build timestamp or absolute paths: two builds of the
   same commit with the same toolchain on the same target produce identical
   SHA-256 (verify with SHA256SUMS.txt).
4. Host verification status: $Coverage for $Target.
5. Release signature policy: the detached Ed25519 signature over
   SHA256SUMS.txt is produced with the operator key for public releases and
   the clearly-marked dev key for reproducibility runs (reference/keys/README.md).
"@

# ---- checksums + signature ----------------------------------------------
$checksums = @()
Get-ChildItem -Path $bundleDir -File -Recurse | ForEach-Object {
    $rel = $_.FullName.Substring($bundleDir.Length).TrimStart('\', '/').Replace('\', '/')
    $checksums += ("{0}  {1}" -f (Get-Sha256 $_.FullName), $rel)
}
$checksums = $checksums | Sort-Object
$checksumsText = ($checksums -join "`n") + "`n"
Write-Utf8NoBom (Join-Path $bundleDir "SHA256SUMS.txt") $checksumsText

# detached Ed25519 signature over the checksums
$sigFile = Join-Path $bundleDir "SHA256SUMS.txt.sig"
& openssl pkeyutl -sign -inkey $keyPath -rawin -in (Join-Path $bundleDir "SHA256SUMS.txt") -out $sigFile
if ($LASTEXITCODE -ne 0) { throw "openssl signing failed" }
$verifyOut = & openssl pkeyutl -verify -pubin -inkey $pubKeyPath -rawin -in (Join-Path $bundleDir "SHA256SUMS.txt") -sigfile $sigFile 2>&1
if ($LASTEXITCODE -ne 0 -or ($verifyOut -notmatch "Verified OK" -and $verifyOut -notmatch "Verified Successfully")) {
    throw "signature self-verification failed: $verifyOut"
}

# ---- summary -------------------------------------------------------------
$bundleFiles = Get-ChildItem -Path $bundleDir -File -Recurse
$summary = [ordered]@{
    bundle = $bundleDir
    target = $Target
    channel = $Channel
    coverage = $Coverage
    binary_sha256 = $binSha
    files = @($bundleFiles | ForEach-Object {
        $_.FullName.Substring($bundleDir.Length).TrimStart('\', '/').Replace('\', '/')
    })
    signature = "Verified OK (Ed25519, role=$keyRole)"
    sbom = "packaging/sbom.json ($( (Get-Content (Join-Path $root 'packaging\sbom.json') -Raw | ConvertFrom-Json).dependencies.Count ) dependencies)"
}
$summary | ConvertTo-Json -Depth 8
Write-Host "bundle published: $bundleDir" -ForegroundColor Green
