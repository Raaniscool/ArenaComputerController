$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python Launcher 'py' was not found. Install Python 3.10 or later, then rerun this script."
}
$PythonVersionText = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) { throw "Python 3 could not be started with the 'py' launcher." }
try { $PythonVersion = [version]$PythonVersionText } catch { throw "Could not determine the Python 3 version." }
if ($PythonVersion -lt [version]"3.10") {
    throw "Python 3.10 or later is required; found Python $PythonVersionText."
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating an isolated repository virtual environment..."
    & py -3 -m venv (Join-Path $RepoRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the repository virtual environment." }
}
& $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "The repository virtual environment must use Python 3.10 or later." }

Write-Host "Installing the existing controller package and only the screenshot dependencies..."
& $VenvPython -m pip install -e $RepoRoot "Pillow>=10.0" "mss>=9.0"
if ($LASTEXITCODE -ne 0) { throw "Gate 2 setup failed while installing Python packages." }

Write-Host "Checking the companion command surface..."
& $VenvPython -m arena_computer_controller.gate2 --help
if ($LASTEXITCODE -ne 0) { throw "Gate 2 command check failed." }

Write-Host "Setup complete. No cloud resource or Supabase setting was changed."
Write-Host "Next: run .\scripts\run_gate2_windows.ps1 -Configure, then -Pair after the authorized test branch migration is applied."
