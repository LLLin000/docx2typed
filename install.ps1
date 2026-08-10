[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Venv = ".venv",
    [switch]$Editable,
    [switch]$PrintMcpConfig
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Invoke-Native {
    param(
        [string]$File,
        [string[]]$Arguments
    )

    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "command failed ($LASTEXITCODE): $File $($Arguments -join ' ')"
    }
}

Invoke-Native $Python @("--version")
Invoke-Native $Python @("-c", "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'")

$pythonExe = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    if (Test-Path $Venv) {
        throw "existing path is not a Python virtual environment: $Venv"
    }
    Invoke-Native $Python @("-m", "venv", $Venv)
}
if (-not (Test-Path $pythonExe)) {
    throw "virtual environment was not created: $pythonExe"
}

$installArgs = if ($Editable) {
    @("-e", $root)
} else {
    @("--upgrade", "docx2typed")
}
Invoke-Native $pythonExe (@("-m", "pip", "install") + $installArgs)
Invoke-Native $pythonExe @("-c", "import sys; assert sys.version_info >= (3, 11), 'virtual environment uses Python older than 3.11'")

$cliExe = Join-Path $Venv "Scripts\docx2typed.exe"
$mcpExe = Join-Path $Venv "Scripts\docx2typed-mcp.exe"
$reviewExe = Join-Path $Venv "Scripts\docx2typed-review.exe"
foreach ($entryPoint in @($cliExe, $mcpExe, $reviewExe)) {
    if (-not (Test-Path $entryPoint)) {
        throw "installed entry point is missing: $entryPoint"
    }
}
Invoke-Native $cliExe @("extract", "--help")
Invoke-Native $cliExe @("review", "--help")
Invoke-Native $reviewExe @("--help")

$resolvedVenv = (Resolve-Path $Venv).Path
$version = & $pythonExe -c "import importlib.metadata as m; print(m.version('docx2typed'))"
Write-Host "Installed docx2typed $version into $resolvedVenv"
$activate = if ([IO.Path]::IsPathRooted($Venv)) {
    "& $resolvedVenv\Scripts\Activate.ps1"
} else {
    ".\$Venv\Scripts\Activate.ps1"
}
Write-Host "Activate with: $activate"

if ($PrintMcpConfig) {
    $config = [ordered]@{
        mcpServers = [ordered]@{
            docx2typed = [ordered]@{
                command = (Resolve-Path $pythonExe).Path
                args = @("-m", "docx2typed", "mcp")
            }
        }
    }
    $config | ConvertTo-Json -Depth 5
}
