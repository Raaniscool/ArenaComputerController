param(
    [switch]$Configure,
    [switch]$Pair,
    [switch]$Status,
    [switch]$Unpair,
    [switch]$LocalTest
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Gate 2 is not installed. Run .\scripts\setup_gate2_windows.ps1 first."
}

$SelectedCount = 0
if ($Configure) { $SelectedCount++ }
if ($Pair) { $SelectedCount++ }
if ($Status) { $SelectedCount++ }
if ($Unpair) { $SelectedCount++ }
if ($LocalTest) { $SelectedCount++ }
if ($SelectedCount -gt 1) {
    throw "Choose at most one of -Configure, -Pair, -Status, -Unpair, or -LocalTest."
}

$Command = "run"
if ($LocalTest) { $Command = "local-test" }
elseif ($Configure) { $Command = "configure" }
elseif ($Pair) { $Command = "pair" }
elseif ($Status) { $Command = "status" }
elseif ($Unpair) { $Command = "unpair" }

Set-Location $RepoRoot
& $VenvPython -m arena_computer_controller.gate2 $Command
exit $LASTEXITCODE
