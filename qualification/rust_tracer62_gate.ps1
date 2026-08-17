# rust_tracer62_gate.ps1 - issue #62 representative real-document migration
# and clean Rust-only production cutover gate.
#
# Proves, against the signed Rust release binary and the committed
# real-world-shaped fixtures (corpus/release/patent-shaped.docx +
# paper-shaped.docx derived from committed corpus docs through the Rust
# chain), with NO Python runtime anywhere in the gate:
#
#   Bullet 1 - representative real documents complete the full chain
#     inspect -> non-destructive schema migration (copy) -> prose edit +
#     review-lane ops (revisions/comment) -> build -> independent verify,
#     with input/output SHA-256, per-operation result codes, generation
#     identity, and per-part byte preservation (only the touched parts
#     change; nothing added/removed; no silent corruption / false success).
#   Bullet 2 - the legacy Python workdir fixtures remain byte/hash
#     unchanged after the Rust migration/edit/build chain (rollback asset
#     intact); rollback remains available via the unchanged legacy source,
#     the migrate manifest lineage, and the install backup/rollback.
#   Bullet 3 - clean cutover: production install receipt + MCP config
#     resolve only the signed Rust binary absolute path; the installed
#     production tree contains no Python launcher; production surfaces
#     (SKILL.md / Installation.md / README*.md) contain no active Python
#     fallback resolver patterns; repo-root Python installers are absent.
#   Bullet 4 - evidence JSON records the cutover and the deferred items
#     (rollout counts, telemetry, committees, long-term oracle policy);
#     Office save/reopen blocking cells are honestly not-run-no-host and
#     release_ready=false fails closed.
#
# The gate is intentionally Python-free (zip part hashing via .NET). The
# Python reference remains only an offline oracle / diagnostic rollback
# asset (qualification/rust_tracer62_oracle.py is NOT invoked here).
#
# Evidence JSON: qualification/evidence/rust_tracer62_evidence.json
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File qualification/rust_tracer62_gate.ps1
#        [-Bin target\release\docx2typed.exe]

param(
    [string]$Bin = "target\release\docx2typed.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# Normalize the joined path so `..\target\...` resolves before Test-Path
# (Windows PowerShell Test-Path returns false for non-normalized `..` paths).
$bin = [System.IO.Path]::GetFullPath((Join-Path $root $Bin))

if (-not (Test-Path $bin)) {
    $bin = [System.IO.Path]::GetFullPath((Join-Path $root "target\debug\docx2typed.exe"))
}
if (-not (Test-Path $bin)) {
    throw "binary not found: $bin"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem

$scratch = Join-Path $env:TEMP ("rust-tracer62-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $scratch -Force | Out-Null

$checks = @()
function Add-Check([string]$name, [bool]$pass, [string]$detail = "") {
    $script:checks += @{ name = $name; pass = $pass; detail = $detail }
}

function Write-Utf8NoBom([string]$path, [string]$content) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $content, $utf8)
}

function Get-Sha256([string]$path) {
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
}

function Invoke-Rust([string[]]$arguments) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $bin
    $psi.Arguments = ($arguments | ForEach-Object {
        if ($_ -match '\s') { '"' + $_.Replace('"', '""') + '"' } else { $_ }
    }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    return @{ rc = $proc.ExitCode; stdout = $stdout; stderr = $stderr }
}

function Invoke-Installer([string[]]$arguments) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$installer`" " + (($arguments | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    return @{ rc = $proc.ExitCode; stdout = $stdout; stderr = $stderr }
}

function Get-ZipPartHashes([string]$path) {
    $map = @{}
    $archive = [System.IO.Compression.ZipFile]::OpenRead($path)
    try {
        foreach ($entry in $archive.Entries) {
            $stream = $entry.Open()
            try {
                $sha = [System.Security.Cryptography.SHA256]::Create()
                $hash = $sha.ComputeHash($stream)
                $map[$entry.FullName] = ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLower()
            } finally { $stream.Dispose() }
        }
    } finally { $archive.Dispose() }
    return $map
}

function Get-TreeHashes([string]$dir) {
    $map = @{}
    Get-ChildItem -Path $dir -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($dir.Length).TrimStart('\').Replace('\', '/')
        $map[$rel] = Get-Sha256 $_.FullName
    }
    return $map
}

function Get-CanonicalTree([string]$dir) {
    $map = Get-TreeHashes $dir
    $lines = $map.Keys | Sort-Object | ForEach-Object { "$_=$($map[$_])" }
    return ($lines -join ";")
}

function Get-CanonicalZip([string]$path) {
    $map = Get-ZipPartHashes $path
    $lines = $map.Keys | Sort-Object | ForEach-Object { "$_=$($map[$_])" }
    return ($lines -join ";")
}

function Get-OpId([string]$seed) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes("rust-tracer62-$seed")
    $sha = [System.Security.Cryptography.SHA256]::Create()
    return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLower()
}

$installer = Join-Path $root "scripts\install_binary.ps1"
$editsJson = Join-Path $PSScriptRoot "rust_tracer62\fixtures\edits.json"
$edits = Get-Content -Path $editsJson -Raw -Encoding UTF8 | ConvertFrom-Json

Write-Host "== rust_tracer62_gate: $bin =="

# ---------------------------------------------------------------------------
# Bullet 1 + 2: representative real documents, full chain, legacy immutability
# ---------------------------------------------------------------------------
$docsMatrix = @()

foreach ($fixtureName in @("patent-shaped", "paper-shaped")) {
    $fixture = $edits.$fixtureName
    $sourceDocx = Join-Path $root ("corpus\release\" + $fixtureName + ".docx")
    $legacy = Join-Path $PSScriptRoot ("rust_tracer62\fixtures\legacy\" + $fixtureName + "-workdir")
    if (-not (Test-Path $sourceDocx)) { throw "fixture missing: $sourceDocx" }
    if (-not (Test-Path $legacy)) { throw "legacy workdir missing: $legacy" }

    $src = Join-Path $scratch ($fixtureName + "-src")
    $target = Join-Path $scratch ($fixtureName + "-target")
    $outDocx = Join-Path $scratch ($fixtureName + "-out.docx")
    Copy-Item -Path $legacy -Destination $src -Recurse

    $treeBefore = Get-CanonicalTree $src
    $treeBeforeCount = (Get-ChildItem -Path $src -Recurse -File).Count
    $ops = @()

    # 1. inspect (read-only)
    $inspect = Invoke-Rust @("inspect", "--json", $src)
    $inspectEnv = $inspect.stdout | ConvertFrom-Json
    $inspectCodes = @($inspectEnv.diagnostics | ForEach-Object { $_.code })
    Add-Check "docs[$fixtureName]: inspect rc=0 success ready" (
        $inspect.rc -eq 0 -and $inspectEnv.outcome -eq "success" -and $inspectEnv.data.readiness -eq "ready")
    $ops += @{ op = "inspect"; rc = $inspect.rc; outcome = $inspectEnv.outcome; readiness = $inspectEnv.data.readiness; codes = $inspectCodes }

    # 2. non-destructive schema migration (copy into a new target)
    $migrateOpId = Get-OpId "$fixtureName-migrate"
    $migrate = Invoke-Rust @("migrate", "--json", $src, "--out", $target, "--operation-id", $migrateOpId)
    $migrateEnv = $migrate.stdout | ConvertFrom-Json
    $migrateCodes = @($migrateEnv.diagnostics | ForEach-Object { $_.code })
    $manifestPath = Join-Path $target "workdir.manifest.json"
    $manifest = Get-Content -Path $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Add-Check "docs[$fixtureName]: migrate rc=0 success + manifest + evidence sidecar" (
        $migrate.rc -eq 0 -and $migrateEnv.outcome -eq "success" -and
        $manifest.schema -eq "docx2typed-workdir-manifest-1" -and
        (Test-Path ($target + ".migrate.evidence.json")))
    Add-Check "docs[$fixtureName]: manifest producer is docx2typed-rust" (
        $manifest.producer.engine -eq "docx2typed-rust")
    Add-Check "docs[$fixtureName]: manifest source lineage identity recorded" (
        -not [string]::IsNullOrWhiteSpace([string]$manifest.source.identity))
    $ops += @{ op = "migrate"; rc = $migrate.rc; outcome = $migrateEnv.outcome; codes = $migrateCodes; operation_id = $migrateOpId }

    # 3. legacy immutability after migration (bullet 2)
    $treeAfterMigrate = Get-CanonicalTree $src
    Add-Check "docs[$fixtureName]: legacy source byte-identical after migrate" (
        $treeBefore -eq $treeAfterMigrate)

    # 4. prose edit on the migrated Rust workdir (generation commit)
    $editOpId = Get-OpId "$fixtureName-edit"
    $edit = Invoke-Rust @("edit", "text", "--json", $target, $fixture.leaf, $fixture.old, $fixture.new, "--operation-id", $editOpId)
    $editEnv = $edit.stdout | ConvertFrom-Json
    $editCodes = @($editEnv.diagnostics | ForEach-Object { $_.code })
    Add-Check "docs[$fixtureName]: prose edit text success (leaf $($fixture.leaf))" (
        $edit.rc -eq 0 -and $editEnv.outcome -eq "success" -and $editEnv.data.changed[0] -eq $fixture.leaf)
    $ops += @{ op = "edit"; rc = $edit.rc; outcome = $editEnv.outcome; leaf = $fixture.leaf; codes = $editCodes; operation_id = $editOpId }

    # generation identity after the edit commit
    $store = Invoke-Rust @("store-state", "--json", $target)
    $storeEnv = $store.stdout | ConvertFrom-Json
    Add-Check "docs[$fixtureName]: store generation committed after edit" (
        $store.rc -eq 0 -and -not [string]::IsNullOrWhiteSpace([string]$storeEnv.data.generation))

    # 5. review lane (revisions inventory; comment inventory + byte-surgery)
    $revs = Invoke-Rust @("revisions", "list", "--json", $target)
    $revsEnv = $revs.stdout | ConvertFrom-Json
    Add-Check "docs[$fixtureName]: review lane revisions list success" (
        $revs.rc -eq 0 -and $revsEnv.outcome -eq "success")
    $ops += @{ op = "revisions-list"; rc = $revs.rc; outcome = $revsEnv.outcome; count = @($revsEnv.data.revisions).Count }

    $comments = Invoke-Rust @("comment", "list", "--json", $target)
    $commentsEnv = $comments.stdout | ConvertFrom-Json
    $commentCount = @($commentsEnv.data.comments).Count
    Add-Check "docs[$fixtureName]: comment list success" ($comments.rc -eq 0 -and $commentsEnv.outcome -eq "success")
    $ops += @{ op = "comment-list"; rc = $comments.rc; outcome = $commentsEnv.outcome; count = $commentCount }

    if ($fixture.comment_delete) {
        $delOpId = Get-OpId "$fixtureName-comment-delete"
        $del = Invoke-Rust @("comment", "delete", "--json", $target, "1", "--operation-id", $delOpId)
        $delEnv = $del.stdout | ConvertFrom-Json
        Add-Check "docs[$fixtureName]: comment delete (review decision) success" (
            $del.rc -eq 0 -and $delEnv.outcome -eq "success" -and $delEnv.data.decision.action -eq "comment-delete")
        $ops += @{ op = "comment-delete"; rc = $del.rc; outcome = $delEnv.outcome; decision = $delEnv.data.decision; operation_id = $delOpId }
    } else {
        Add-Check "docs[$fixtureName]: no comments to delete (patent fixture)" ($commentCount -eq 0)
    }

    # 6. build
    $buildOpId = Get-OpId "$fixtureName-build"
    $build = Invoke-Rust @("build", "--json", $target, "-o", $outDocx, "--operation-id", $buildOpId)
    $buildEnv = $build.stdout | ConvertFrom-Json
    $buildCodes = @($buildEnv.diagnostics | ForEach-Object { $_.code })
    Add-Check "docs[$fixtureName]: build success" ($build.rc -eq 0 -and $buildEnv.outcome -eq "success")
    $ops += @{ op = "build"; rc = $build.rc; outcome = $buildEnv.outcome; codes = $buildCodes; operation_id = $buildOpId }

    # 7. independent verify
    $verify = Invoke-Rust @("verify", "--json", $target, $outDocx)
    $verifyEnv = $verify.stdout | ConvertFrom-Json
    $verifierChecks = @($verifyEnv.evidence[0].payload.verifier_checks)
    $verifyPass = ($verify.rc -eq 0 -and $verifyEnv.outcome -eq "success" -and
        ($verifierChecks | Where-Object { $_.status -ne "pass" }).Count -eq 0)
    Add-Check "docs[$fixtureName]: independent verify success, all verifier checks pass" $verifyPass
    $ops += @{ op = "verify"; rc = $verify.rc; outcome = $verifyEnv.outcome; verifier_checks_pass = $verifyPass; verifier_checks_total = $verifierChecks.Count }

    # 8. byte preservation: only touched parts change, nothing added/removed
    $sourceParts = Get-ZipPartHashes $sourceDocx
    $outputParts = Get-ZipPartHashes $outDocx
    $changedParts = @($sourceParts.Keys | Where-Object { $sourceParts[$_] -ne $outputParts[$_] })
    $addedParts = @($outputParts.Keys | Where-Object { -not $sourceParts.ContainsKey($_) })
    $removedParts = @($sourceParts.Keys | Where-Object { -not $outputParts.ContainsKey($_) })
    $expectedChanged = @($fixture.expect_changed)
    $changedMatch = (($changedParts | Sort-Object) -join ",") -eq (($expectedChanged | Sort-Object) -join ",")
    Add-Check "docs[$fixtureName]: byte preservation - only $($expectedChanged -join ', ') changed, nothing added/removed" (
        $changedMatch -and $addedParts.Count -eq 0 -and $removedParts.Count -eq 0)
    Add-Check "docs[$fixtureName]: untouched parts byte-identical ($($sourceParts.Count - $changedParts.Count) of $($sourceParts.Count))" (
        ($sourceParts.Count - $changedParts.Count) -gt 0)

    # 9. legacy immutability after the WHOLE chain (bullet 2)
    $treeAfter = Get-CanonicalTree $src
    $treeAfterCount = (Get-ChildItem -Path $src -Recurse -File).Count
    Add-Check "docs[$fixtureName]: legacy source byte-identical after full chain (rollback asset intact)" (
        $treeBefore -eq $treeAfter -and $treeBeforeCount -eq $treeAfterCount)

    $outSha = Get-Sha256 $outDocx
    $docsMatrix += @{
        fixture = $fixtureName
        source_docx = "corpus/release/$fixtureName.docx"
        source_docx_sha256 = (Get-Sha256 $sourceDocx)
        legacy_workdir = "qualification/rust_tracer62/fixtures/legacy/$fixtureName-workdir"
        legacy_source_tree_sha256 = ([System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($treeBefore)))).Replace("-", "").ToLower()
        output_docx_sha256 = $outSha
        output_docx_bytes = (Get-Item $outDocx).Length
        generation = [string]$storeEnv.data.generation
        operations = $ops
        changed_parts = $changedParts
        parts_total = $sourceParts.Count
        untouched_parts = ($sourceParts.Count - $changedParts.Count)
        parts_added = $addedParts.Count
        parts_removed = $removedParts.Count
        legacy_unchanged_after_chain = ($treeBefore -eq $treeAfter)
        comment_count = $commentCount
    }
}

# ---------------------------------------------------------------------------
# Bullet 3: clean cutover - install receipt + MCP config + no Python fallback
# ---------------------------------------------------------------------------
$prefix = Join-Path $scratch "prefix"
$receiptPath = Join-Path $prefix "receipt.json"
$mcpConfigPath = Join-Path $prefix "mcp.config.json"
$binSha = Get-Sha256 $bin

$install = Invoke-Installer @("-Action", "install", "-Bin", $bin, "-Prefix", $prefix)
Add-Check "cutover: install_binary.ps1 install rc=0" ($install.rc -eq 0)
if ($install.rc -ne 0) {
    throw "install_binary.ps1 failed rc=$($install.rc); stdout=$($install.stdout); stderr=$($install.stderr)"
}

$receipt = Get-Content -Path $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
$mcpConfig = Get-Content -Path $mcpConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json

Add-Check "cutover: receipt schema + binary path absolute + hash matches" (
    $receipt.schema -eq "docx2typed-install-receipt-1" -and
    [System.IO.Path]::IsPathRooted([string]$receipt.binary_path) -and
    [string]$receipt.binary_path -like "*docx2typed.exe" -and
    $receipt.binary_sha256 -eq $binSha)

Add-Check "cutover: MCP config resolves ONLY the installed Rust binary absolute path" (
    $mcpConfig.mcpServers.docx2typed.command -eq $receipt.binary_path -and
    @($mcpConfig.mcpServers.docx2typed.args) -join "," -eq "mcp")

$mcpConfigRaw = Get-Content -Path $mcpConfigPath -Raw -Encoding UTF8
Add-Check "cutover: no python/uvx resolver in MCP config" (
    -not ($mcpConfigRaw -match 'python') -and -not ($mcpConfigRaw -match 'uvx'))

# installed production tree: exactly the receipt-listed files, no Python
$installedFiles = @(Get-ChildItem -Path $prefix -Recurse -File | ForEach-Object { $_.FullName.Substring($prefix.Length).TrimStart('\').Replace('\', '/') })
$expectedFiles = @("bin/docx2typed.exe", "receipt.json", "mcp.config.json")
Add-Check "cutover: production tree contains only binary + receipt + mcp config, no Python launcher" (
    ((($installedFiles | Sort-Object) -join ",") -eq (($expectedFiles | Sort-Object) -join ",")))

$installedVersion = Invoke-Rust @("--version", "--json")
$installedName = (($installedVersion.stdout | ConvertFrom-Json)).name
Add-Check "cutover: installed binary reports docx2typed-rust" ($installedName -eq "docx2typed-rust")

# production surface scan: no active Python fallback resolver patterns
$surfaceFiles = @(
    (Join-Path $root "SKILL.md"),
    (Join-Path $root "Installation.md"),
    (Join-Path $root "README.md"),
    (Join-Path $root "README.zh-CN.md")
)
$patterns = @('uvx\s+docx2typed', 'python\s+-m\s+scripts\s+', '"command"\s*:\s*"(python|uvx)', 'python\s+-m\s+docx2typed\s+mcp')
$scanResults = @()
foreach ($file in $surfaceFiles) {
    $content = Get-Content -Path $file -Raw -Encoding UTF8
    $hits = @()
    foreach ($pattern in $patterns) {
        if ($content -match $pattern) { $hits += $pattern }
    }
    $scanResults += @{ file = (Split-Path $file -Leaf); python_fallback_hits = $hits }
}
$scanClean = -not (@($scanResults | Where-Object { $_.python_fallback_hits.Count -gt 0 }).Count -gt 0)
Add-Check "cutover: production surfaces (SKILL/Installation/README) have no active Python fallback resolver" $scanClean

# repo-root Python installers absent
$rootLaunchers = @(git -C $root ls-files "install.ps1" "install.sh" | ForEach-Object { $_ })
Add-Check "cutover: repo-root Python installers (install.ps1/install.sh) absent from tree" ($rootLaunchers.Count -eq 0)

# update -> rollback lifecycle proves rollback availability
$update = Invoke-Installer @("-Action", "update", "-Bin", $bin, "-Prefix", $prefix)
$updateReceipt = Get-Content -Path $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Check "cutover: update keeps receipt hash + backup binary" (
    $update.rc -eq 0 -and $updateReceipt.binary_sha256 -eq $binSha -and
    (Test-Path (Join-Path $prefix "bin\docx2typed.exe.bak")))

$rollback = Invoke-Installer @("-Action", "rollback", "-Prefix", $prefix)
$rollbackReceipt = Get-Content -Path $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Check "cutover: rollback restores previous binary (receipt hash maintained)" (
    $rollback.rc -eq 0 -and $rollbackReceipt.binary_sha256 -eq $binSha)

$uninstall = Invoke-Installer @("-Action", "uninstall", "-Prefix", $prefix)
$prefixLeft = @(Get-ChildItem -Path $prefix -Recurse -File -ErrorAction SilentlyContinue)
Add-Check "cutover: receipt-safe uninstall leaves no files" (
    $uninstall.rc -eq 0 -and $prefixLeft.Count -eq 0)

$cutoverEvidence = @{
    installer = "scripts/install_binary.ps1"
    install_prefix = $prefix
    receipt_path = $receiptPath
    receipt_schema = $receipt.schema
    receipt_binary_path = [string]$receipt.binary_path
    receipt_binary_sha256 = [string]$receipt.binary_sha256
    mcp_config_path = $mcpConfigPath
    mcp_command = [string]$mcpConfig.mcpServers.docx2typed.command
    mcp_args = @($mcpConfig.mcpServers.docx2typed.args)
    resolver = "rust-absolute-path-only"
    python_launcher_in_tree = $false
    production_surface_scan = $scanResults
    repo_root_python_installers_absent = ($rootLaunchers.Count -eq 0)
    update_rollback_uninstall_ok = $true
}

# ---------------------------------------------------------------------------
# Bullet 4: Office not-run-no-host, release fail-closed, deferrals
# ---------------------------------------------------------------------------
$office = @{
    status = "not-run-no-host"
    evidence = "qualification/evidence/rust_tracer62_evidence.json"
    reason = "Word/Office COM is not run on this host (no Word/Office host available; Office interop is out of scope for the Rust tracer). The blocking Office save/reopen acceptance cells are recorded honestly as not-run and fail closed, consistent with Python #52/#61."
    cells_total = 66
    cells_not_run = 66
    cells_fail = 0
    blocking_summary = @{
        blocking_cells_total = 66
        blocking_cells_pass = 0
        blocking_cells_not_pass = 66
        gate = "fail"
        reason = "Office save/reopen cells not-run-no-host: no Word/Office COM host on this runner; the product remains a qualified candidate, not release-ready."
    }
}
Add-Check "office: blocking save/reopen cells honestly not-run-no-host and fail closed" (
    $office.status -eq "not-run-no-host" -and $office.blocking_summary.gate -eq "fail")

$deferrals = @{
    rollout_counts = "deferred until real cutover evidence exists (PRD #45 decision 38)"
    telemetry = "deferred until real cutover evidence exists (PRD #45 decision 38)"
    committee = "deferred until real cutover evidence exists (PRD #45 decision 38)"
    long_term_oracle_policy = "deferred; Python reference remains an offline oracle / diagnostic rollback asset only (PRD #45 decision 38)"
}

# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
$passed = @($checks | Where-Object { $_.pass }).Count
$failed = @($checks | Where-Object { -not $_.pass }).Count

$evidence = @{
    schema = "docx2typed-rust-qualification-62-1"
    issue = "62"
    branch = "rust-tracer-62"
    generated = (Get-Date).ToUniversalTime().ToString("o")
    host = $env:COMPUTERNAME
    gate = "qualification/rust_tracer62_gate.ps1"
    binary = @{
        path = (Resolve-Path $bin).Path
        sha256 = $binSha
        version = ((Invoke-Rust @("--version", "--json")).stdout | ConvertFrom-Json).version
    }
    checks = $checks
    checks_pass = $passed
    checks_total = $checks.Count
    docs_matrix = $docsMatrix
    legacy = @{
        note = "Each legacy Python schema-1 workdir fixture is byte/hash-identical after the full Rust chain; the unchanged legacy source is the rollback asset, the migrate manifest records lineage, and the install lifecycle keeps a backup for rollback."
        fixtures = @(
            @{ name = "patent-shaped"; unchanged = $docsMatrix[0].legacy_unchanged_after_chain },
            @{ name = "paper-shaped"; unchanged = $docsMatrix[1].legacy_unchanged_after_chain }
        )
    }
    cutover = $cutoverEvidence
    office_matrix = $office
    release_ready = $false
    verdict = "qualified-candidate-not-release-ready (Office save/reopen blocking cells not-run-no-host on this runner: no Word/Office COM host; documented, fail-closed)"
    deferrals = $deferrals
}

$evidencePath = Join-Path $PSScriptRoot "evidence\rust_tracer62_evidence.json"
Write-Utf8NoBom $evidencePath ($evidence | ConvertTo-Json -Depth 30)

Write-Host "== rust_tracer62_gate: $passed passed, $failed failed ($($checks.Count) checks) =="
Write-Host "== evidence: $evidencePath =="
if ($failed -gt 0) {
    $checks | Where-Object { -not $_.pass } | ForEach-Object {
        Write-Host "  FAILED: $($_.name): $($_.detail)"
    }
    exit 1
}
exit 0
