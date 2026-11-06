$ErrorActionPreference = 'Stop'
$repositoryRoot = $PSScriptRoot
$previousLocation = (Get-Location).Path
$exitCode = 1

try {
    $python = Get-Command python -ErrorAction Stop
    Set-Location -LiteralPath $repositoryRoot
    & $python.Source -m arena_workshop.addon_updater --repo-root $repositoryRoot
    $exitCode = $LASTEXITCODE
}
catch {
    Write-Error "Could not run the Arena Blender Workshop updater: $_"
    $exitCode = 1
}
finally {
    Set-Location -LiteralPath $previousLocation
}

if ($exitCode -ne 0) {
    throw "Blender add-on update failed (exit code $exitCode). See the diagnostic above."
}
