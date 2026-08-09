[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Venv = ".venv",
    [switch]$Editable
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

& $Python --version
if (-not (Test-Path $Venv)) {
    & $Python -m venv $Venv
}

$pythonExe = Join-Path $Venv "Scripts\python.exe"
$cliExe = Join-Path $Venv "Scripts\docx2typed.exe"
if (-not (Test-Path $pythonExe)) {
    throw "virtual environment was not created: $pythonExe"
}

if ($Editable) {
    & $pythonExe -m pip install -e .
} else {
    & $pythonExe -m pip install .
}
& $cliExe extract --help | Out-Host

Write-Host "Installed docx2typed into $((Resolve-Path $Venv).Path)"
Write-Host "Activate with: .\$Venv\Scripts\Activate.ps1"
