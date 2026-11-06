# First Windows + Blender 5.2 controller test

This procedure exercises the controller's existing Blender mailbox adapter. It does not install or enable the Blender add-on, open a `.blend` project, save a project, or use Google Drive. Run it only on the owner's Windows computer; the coding sandbox cannot perform the live Blender test.

## Preconditions and safety

- Use 64-bit CPython 3.11 for this first run. The project declares Python `>=3.10`; 3.11 is the version used for the sandbox dependency-resolution check.
- Blender 5.2 and the existing **Arena Blender Workshop MVP** add-on must already be installed and enabled by the owner. The controller does not install, update, launch, or enable the add-on. If it is not available in Blender's Add-ons preferences, stop and use the owner's normal add-on installation procedure; do not use this test to update it.
- Start Blender under the same signed-in Windows account as the controller. Both processes must use the same `%LOCALAPPDATA%\ArenaBlenderWorkshop\queue` mailbox.
- Use a new, disposable Blender General scene, not an existing project. The add-on may remove Blender's untouched startup cube. Keep the active camera and do not switch files/scenes during the test. Close Blender without saving at the end.
- Run without elevation. Keep the controller on `127.0.0.1`; do not change firewall, execution-policy, or network security settings. The default loopback setup needs no public-network access.

The controller loads the external ArenaBlenderWorkshop checkout from `ARENA_BLENDER_WORKSHOP_PATH`, which must be its repository root—the folder containing `arena_workshop\__init__.py`. The example below checks `$HOME\Documents\ArenaBlenderWorkshop`; it prompts for another path if needed.

## Terminal A — install and start the controller

Open a PowerShell terminal as the same normal Windows user who will run Blender. Paste this block. When prompted, enter the full controller checkout path. If port 8765 is occupied, do not stop the owning process; change `$port` to an unused local port and use that same port in Terminal B.

```powershell
$controllerRoot = Read-Host 'Full path to the ArenaComputerController checkout'
Set-Location -LiteralPath $controllerRoot
if (-not (Test-Path -LiteralPath (Join-Path $PWD 'pyproject.toml'))) {
    throw 'This folder does not contain the ArenaComputerController pyproject.toml.'
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python Launcher (py) is not available. Install/select 64-bit CPython 3.11 using the owner’s normal process.'
}
py -3.11 --version
if ($LASTEXITCODE -ne 0) { throw 'Python Launcher could not find CPython 3.11.' }
$port = 8765
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $listener | Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table
    throw "Port $port is already listening. Choose an unused port; do not terminate that process."
}

$connectorRoot = Join-Path $HOME 'Documents\ArenaBlenderWorkshop'
$connectorPackage = Join-Path $connectorRoot 'arena_workshop\__init__.py'
if (-not (Test-Path -LiteralPath $connectorPackage)) {
    $connectorRoot = Read-Host 'Full path to the ArenaBlenderWorkshop checkout root'
    $connectorPackage = Join-Path $connectorRoot 'arena_workshop\__init__.py'
}
if (-not (Test-Path -LiteralPath $connectorPackage)) {
    throw "Connector package not found: $connectorPackage"
}

$env:ARENA_BLENDER_WORKSHOP_PATH = (Resolve-Path -LiteralPath $connectorRoot).Path
$env:ARENA_CONTROLLER_HOST = '127.0.0.1'
$env:ARENA_CONTROLLER_PORT = [string]$port

$venv = Join-Path $env:TEMP ('ArenaComputerController-BlenderTest-py311-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
if (-not (Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
    py -3.11 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
$activateScript = Join-Path $venv 'Scripts\Activate.ps1'
try {
    . $activateScript
}
catch {
    Write-Warning 'Activation was blocked; using the virtual-environment Python directly without changing policy.'
}
$venvPython = Join-Path $venv 'Scripts\python.exe'
& $venvPython --version
$venvVersion = & $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $venvVersion -ne '3.11') {
    throw "Expected a Python 3.11 virtual environment; found '$venvVersion'."
}
$venvBits = & $venvPython -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0 -or $venvBits -ne '64') {
    throw "Expected 64-bit Python; found '$venvBits'-bit."
}
& $venvPython -m pip install -e '.[windows]'
if ($LASTEXITCODE -ne 0) { throw 'The Windows dependency installation failed; do not start the controller.' }
& $venvPython -m arena_computer_controller
```

Keep Terminal A open. Startup should report the Blender adapter as configured from the selected checkout and the API at `http://127.0.0.1:8765` (or the alternate port). If PowerShell blocks `Activate.ps1`, the block warns and still invokes the virtual-environment interpreter directly; it does not change execution policy or fall back to system Python.

The `windows` extra installs `pywinauto`, `mss`, Pillow, `psutil`, and `pyautogui`. The project also has a process-list fallback if `psutil` is absent, but install the documented extra for this test. A dry-run of `pip install -e '.[windows]'` resolved successfully under Python 3.11 in the Linux sandbox; that does not replace installing/testing the Windows wheels on the owner's machine.

## Before using Terminal B — open the disposable Blender scene

1. Start Blender 5.2 as the same Windows user. Do not open or overwrite an existing project; use a new General scene and keep its active camera.
2. In Blender Preferences, confirm **Arena Blender Workshop MVP** is installed and enabled (the installed add-ons/extensions page wording can vary by Blender version). If it is not installed, stop rather than changing add-on code as part of this controller test.
3. Leave that scene open for the entire test. The add-on targets the scene captured at initialization. Do not save it; close Blender and discard changes after testing.

## Terminal B — run and verify the workflow

Open a second PowerShell terminal under the same user. Set `$port` below to the value used in Terminal A. If a bearer token was already configured as a user environment variable, the helper uses it without printing it; otherwise the default loopback API needs no token.

```powershell
$port = 8765
$base = "http://127.0.0.1:$port"
$headers = @{}
if ($env:ARENA_CONTROLLER_TOKEN) {
    $headers['Authorization'] = "Bearer $env:ARENA_CONTROLLER_TOKEN"
}

function Invoke-ArenaPost {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][hashtable]$Body,
        [int]$ClientTimeoutSec = 150
    )
    $json = ConvertTo-Json -InputObject $Body -Depth 10 -Compress
    try {
        Invoke-RestMethod -Uri ($base + $Path) -Method Post `
            -ContentType 'application/json' -Headers $headers -Body $json `
            -TimeoutSec $ClientTimeoutSec -ErrorAction Stop
    }
    catch {
        Write-Host "POST $Path failed: $($_.Exception.Message)" -ForegroundColor Red
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            Write-Host $_.ErrorDetails.Message
        }
        throw
    }
}

# 1. Confirm the API is up, then inspect the process-detection entry for Blender.
$health = Invoke-RestMethod -Uri "$base/health" -Headers $headers -TimeoutSec 10
if (-not $health.ok) { throw 'Controller health check failed.' }
$state = Invoke-RestMethod -Uri "$base/v1/state" -Headers $headers -TimeoutSec 20
$blenderState = $state.integrations | Where-Object { $_.name -eq 'Blender' } | Select-Object -First 1
$blenderState | Format-List name, application_detected, connected, status
if (-not $blenderState -or -not $blenderState.application_detected) {
    throw 'Blender process was not detected. Confirm Blender 5.2 is open, then inspect the process list.'
}

# 2. Probe add-on responsiveness. This POST submits hello_test internally.
$probe = Invoke-ArenaPost -Path '/v1/integrations/blender/probe' `
    -Body @{ timeout = 15 } -ClientTimeoutSec 30
$probe | Select-Object configured, addon_responding, healthy, blender_version, active_scene | Format-List
if (-not $probe.addon_responding -or -not $probe.healthy -or
    ([string]$probe.blender_version -notlike '5.2*')) {
    throw 'hello_test did not return a healthy Blender 5.2 result.'
}

# 3. Create a uniquely named cube. Keep this request object/ID if retrying after a timeout.
$objectName = 'ArenaTestCube_' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$createRequest = @{
    operation = 'create_primitive'
    parameters = @{
        primitive_type = 'cube'
        object_name = $objectName
        location = @(0.0, 0.0, 0.0)
    }
    timeout = 30
    request_id = [guid]::NewGuid().ToString()
}
$created = Invoke-ArenaPost -Path '/v1/integrations/blender/commands' `
    -Body $createRequest -ClientTimeoutSec 45
$created | ConvertTo-Json -Depth 10
if (-not $created.ok -or $created.status -ne 'completed' -or
    -not $created.result.created -or $created.result.object_name -ne $objectName) {
    throw 'create_primitive did not return the expected successful result.'
}

# 4. Modify that same primitive's dimensions; the operation requires object_name and a 3-number list.
$dimensionsRequest = @{
    operation = 'set_dimensions'
    parameters = @{
        object_name = $objectName
        dimensions = @(3.0, 2.0, 1.0)
    }
    timeout = 30
    request_id = [guid]::NewGuid().ToString()
}
$modified = Invoke-ArenaPost -Path '/v1/integrations/blender/commands' `
    -Body $dimensionsRequest -ClientTimeoutSec 45
$modified | ConvertTo-Json -Depth 10
$actualDimensions = @($modified.result.dimensions | ForEach-Object { [double]$_ })
if (-not $modified.ok -or $modified.status -ne 'completed' -or
    -not $modified.result.updated -or $modified.result.object_name -ne $objectName -or
    $actualDimensions.Count -ne 3 -or $actualDimensions[0] -ne 3.0 -or
    $actualDimensions[1] -ne 2.0 -or $actualDimensions[2] -ne 1.0) {
    throw 'set_dimensions did not return the expected successful result.'
}

# 5. Render the active camera and retrieve the PNG directly from the controller (no Drive upload).
$renderRequest = @{
    operation = 'capture_screenshot'
    parameters = @{ width = 640; height = 480 }
    timeout = 120
    request_id = [guid]::NewGuid().ToString()
}
$render = Invoke-ArenaPost -Path '/v1/integrations/blender/commands' `
    -Body $renderRequest -ClientTimeoutSec 150
$render | ConvertTo-Json -Depth 10
if (-not $render.ok -or $render.status -ne 'completed' -or
    -not $render.result.screenshot_available -or
    -not $render.result.screenshot_url) {
    throw 'capture_screenshot did not return an available camera-render PNG.'
}
$render.result | Select-Object scene_name, camera_name, width, height, render_engine, size_bytes | Format-List
if ($render.result.width -ne 640 -or $render.result.height -ne 480) {
    throw 'Render dimensions did not match the request.'
}
$pngPath = Join-Path $env:TEMP ("ArenaComputerController-camera-render-" + $render.job_id + '.png')
Invoke-WebRequest -Uri ($base + $render.result.screenshot_url) `
    -Headers $headers -OutFile $pngPath -TimeoutSec 30 -UseBasicParsing -ErrorAction Stop
if (-not (Test-Path -LiteralPath $pngPath)) { throw "PNG was not downloaded: $pngPath" }
$pngBytes = [System.IO.File]::ReadAllBytes($pngPath)
if ($pngBytes.Length -lt 8) { throw 'Downloaded file is too short to be a PNG.' }
$pngSignature = [BitConverter]::ToString($pngBytes, 0, 8)
if ($pngSignature -ne '89-50-4E-47-0D-0A-1A-0A') { throw "Invalid PNG signature: $pngSignature" }
$pngHash = (Get-FileHash -LiteralPath $pngPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($pngHash -ne $render.result.sha256) { throw 'Downloaded PNG SHA-256 does not match Blender result metadata.' }
Start-Process -FilePath $pngPath
Write-Host "Opened camera-render PNG: $pngPath"

# 6. Final machine-checkable summary. Inspect the image visually before reporting success.
$checks = [ordered]@{
    ApiHealthy = [bool]$health.ok
    BlenderProcessDetected = [bool]$blenderState.application_detected
    HelloTestHealthy = [bool]$probe.healthy
    PrimitiveCreated = [bool]$created.result.created
    PrimitiveDimensionsUpdated = [bool]$modified.result.updated
    CameraRenderCompleted = [bool]$render.ok
    PngDownloadedAndHashVerified = [bool]($pngHash -eq $render.result.sha256)
    PngOpenCommandIssued = $true
}
[pscustomobject]$checks | Format-List
if ($checks.Values -contains $false) { throw 'At least one verification step failed.' }
Write-Host 'All controller + Blender checks passed. Close Blender without saving the disposable scene.' -ForegroundColor Green
```

`GET /v1/state` reports Blender **process detection**; its `connected` field is not the add-on health check. Use the probe's `addon_responding`, `healthy`, and `blender_version` fields to confirm the add-on. Successful modeling responses have `ok: true`, `status: "completed"`, and operation-specific `result` fields (`created: true` for `create_primitive`; `updated: true` and `dimensions: [3.0, 2.0, 1.0]` for `set_dimensions`). A successful render returns `screenshot_available: true`, a relative `screenshot_url`, `sha256`, and the requested dimensions. `GET` on that URL returns `image/png` bytes and the `X-Blender-Capture-Type: camera_render` response header.

## Common failures and what to check

- **Port is already listening / WinError 10048:** Do not kill the listener. Choose another port such as `8766`, set it in Terminal A, restart the controller, and update `$port` in Terminal B.
- **`Blender mailbox adapter: not configured` or `unavailable`:** Check that `ARENA_BLENDER_WORKSHOP_PATH` is set in Terminal A and points to the checkout root containing `arena_workshop\__init__.py`; restart the controller after correcting it. A 503 from an integration route means no adapter loaded.
- **HTTP 401:** The controller has `ARENA_CONTROLLER_TOKEN` set. Use the same existing token in the testing terminal's `Authorization: Bearer ...` header; never paste it into a report.
- **Blender not detected in `/v1/state`:** Confirm Blender 5.2 is open in the same Windows session. Process detection is separate from add-on health.
- **`hello_test` timeout / HTTP 504:** Confirm the add-on is enabled and initialized, both apps run as the same Windows user, and the queue root is `%LOCALAPPDATA%\ArenaBlenderWorkshop\queue`. Preserve a timed-out command's `request_id` for a retry; do not immediately create a new ID or issue the modeling command again.
- **`object_name_conflict` / `object_not_found`:** Use the uniquely generated test name and verify `create_primitive` completed before `set_dimensions`. Do not resend with a new ID until you have inspected the disposable scene.
- **`camera_not_found` / render failure:** Make sure the current disposable scene has an active camera. The requested image is a Blender camera render, not a UI/viewport screenshot.
- **Dependency/import errors:** Confirm Terminal A's virtual environment is active, Python reports 3.11, `pip install -e '.[windows]'` completed, and the connector path is correct. Do not bypass policy or disable security protections.

If a request fails, copy back the HTTP status and response/error text, the controller's relevant startup lines, the `/v1/state` Blender entry, and the probe/operation result. Include `python --version` and the port used. Remove any bearer token; do not send secrets. The test image is optional to share and may contain content from the disposable scene.
