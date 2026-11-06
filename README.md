# ArenaComputerController

A small, local-first Windows desktop-control prototype. It exposes a provider-neutral JSON API for desktop state, Windows UI Automation inputs, actions, and screenshots requested on demand.

## Repository assessment

The current branch already contains an earlier connector commit with ArenaBlenderWorkshop files and a Roblox Studio plugin/server. Those pre-existing files are preserved untouched in this task; no new connector source was copied, and no Roblox-to-controller work is being started. A read-only audit of ArenaBlenderWorkshop commit `6674261b39d9070b93e990276149b14d99b8ee7e` informed the Blender adapter below. That source has no declared license, so this adapter loads it from a separately configured checkout via `ARENA_BLENDER_WORKSHOP_PATH` rather than depending on the copy in this repository. The controller reports Blender/Roblox process detection separately from add-on responsiveness.

The controller remains a thin Windows API with native window/process discovery, optional UI Automation and screenshot dependencies, stale-input protection, action results, a reproducible image-encoding benchmark, and a Blender-only adapter that reuses ArenaBlenderWorkshop's existing file mailbox.

## Prototype status and scope

Implemented:

- Starts a local HTTP API and prints a startup report based on the host's detected processes/windows.
- On Windows, enumerates visible top-level windows, identifies the foreground window, and lists processes without reading command-line arguments or application documents.
- Discovers actionable UI Automation controls through `pywinauto` when installed.
- Assigns revision-bound input IDs. An action against an old revision or disappeared target is rejected instead of being sent to a possibly different control.
- Supports UIA click/focus/type/clear/key actions, window focus/key actions, and coordinate click/scroll fallback actions.
- Returns action success/error, a basic window/application state delta, and a bounded action history. Typed text and key sequences are deliberately not stored in history.
- Captures a full desktop, visible window bounds, or a crop only when `/v1/screenshot` is requested. PNG, JPEG, and WebP are supported when Pillow's encoder is available; scale-down is supported.
- Provides an opt-in Blender adapter that imports the existing external connector's validated job builder, allowlist, mailbox, and result waiter. It probes with `hello_test`, bounds waits, keeps structured failures, deduplicates retries that use the same `request_id`, and returns camera-render PNG bytes directly from the controller.
- Keeps the API and tests dependency-free. Windows automation/image libraries are optional extras; the Blender adapter uses only the connector checkout plus the Python standard library.

Not implemented in this task: integration between this computer controller and the existing Roblox Studio connector, event-driven live updates, automatic input-map diffing, multi-provider AI adapters, a dashboard, and AI-provider latency/token measurements. The Blender adapter has simulated test coverage, but live Windows/Blender 5.2 verification remains a separate required check.

## Run on Windows

Python 3.10 or later is required. From the repository root, in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -e ".[windows]"
python -m arena_computer_controller
```

The API binds to `127.0.0.1:8765` by default. To use a different port:

```powershell
$env:ARENA_CONTROLLER_PORT = "8765"
python -m arena_computer_controller
```

The `windows` extra installs `pywinauto`, `mss`, Pillow, `psutil`, and `pyautogui`. If UI Automation is not installed, state/process discovery still works but `/v1/inputs` returns a warning and no controls. If screenshot dependencies are unavailable, the screenshot endpoint returns a clear dependency error.

For a non-loopback bind, configure a bearer token first; unauthenticated network binds are refused:

```powershell
$env:ARENA_CONTROLLER_TOKEN = "use-a-long-random-secret"
$env:ARENA_CONTROLLER_HOST = "0.0.0.0"
python -m arena_computer_controller
```

All API routes except `/health` then require `Authorization: Bearer <token>`. Prefer loopback unless a remote client genuinely needs access. The server does not enable CORS.

On non-Windows hosts the service can still be started and tested, but it reports process-only discovery; window/input actions and desktop screenshots are Windows-only in this prototype.

### Configure the existing Blender connector (Windows)

The controller does not copy or install ArenaBlenderWorkshop. Set the path to the **root of its separately checked-out repository** (the folder containing `arena_workshop\__init__.py`) before starting the controller:

```powershell
$env:ARENA_BLENDER_WORKSHOP_PATH = Join-Path $HOME "Documents\ArenaBlenderWorkshop"
python -m arena_computer_controller
```

Replace that path if the checkout is elsewhere. Run the controller and Blender 5.2 as the same Windows user/session so both use `%LOCALAPPDATA%\ArenaBlenderWorkshop\queue`. Open Blender with the existing ArenaBlenderWorkshop add-on enabled. The connector's existing initialization can remove the default cube; use a disposable scene and remember it does not automatically save the `.blend` file. No Google Drive setup is used or required by this adapter.

The connector's bounded protocol allowlist is reused as-is: `hello_test`, `create_test_object`, `create_primitive`, `move_object`, `rotate_object`, `scale_object`, `rename_object`, `create_material`, `capture_screenshot`, `duplicate_object`, `delete_object`, and `set_dimensions`. Arbitrary Python or unlisted operations cannot be submitted. The adapter waits at most 540 seconds (60 seconds by default; 5 seconds for a probe). A timeout returns the job ID and does not re-enqueue; repeat the same intent with the same `request_id` to safely check/retrieve it. Reusing an ID with different command data is rejected.

## API

### Read state and discover inputs

```http
GET /health
GET /v1/state
GET /v1/inputs
POST /v1/refresh
GET /v1/history?limit=50
```

`GET /v1/inputs` refreshes the UIA tree and returns a new integer `revision`. Use that same revision with a discovered input ID:

```json
{
  "action": "click",
  "target": "input-3-8a12c3d4e5f6",
  "expected_revision": 3
}
```

`POST /v1/actions` also accepts:

```json
{"action":"type","target":"input-3-...","expected_revision":3,"text":"Search terms"}
{"action":"focus","target":"input-3-...","expected_revision":3}
{"action":"clear","target":"input-3-...","expected_revision":3}
{"action":"press_key","window_id":"win-123abc-17","keys":"^s"}
{"action":"focus_window","window_id":"win-123abc-17"}
{"action":"click_at","x":640,"y":360}
{"action":"scroll_at","x":900,"y":700,"delta":-3}
```

Key sequences use pywinauto notation (for example `{ENTER}`, `^s`, or `%{F4}`). Coordinate actions address the primary display. UIA coverage varies by application; canvas-heavy apps may require the coordinate fallback or a dedicated integration. A successful input dispatch is not the same as application-specific verification.

### Request a screenshot

The endpoint returns image bytes directly (not base64), with capture/encode timing and size in response headers:

```http
GET /v1/screenshot?format=jpeg&quality=82&scale=0.75
GET /v1/screenshot?window_id=win-123abc-17&format=png
GET /v1/screenshot?x=100&y=100&width=800&height=600&format=webp
```

Window capture means cropping the window's **currently visible screen bounds**; it does not capture obscured/off-screen window contents. A crop uses absolute desktop coordinates. The controller does not transmit a screenshot automatically after an action.

### Blender add-on probe, modeling commands, and camera render

`GET /v1/state` reports whether a Blender process is detected. Detection is not proof that the add-on is responding; probe it with the harmless `hello_test` mailbox job:

```powershell
$base = "http://127.0.0.1:8765"
Invoke-RestMethod -Method Post -Uri "$base/v1/integrations/blender/probe" `
  -ContentType "application/json" -Body '{"timeout":8}'
```

Submit only allowlisted, schema-validated operations through the separate Blender integration route (not generic `/v1/actions`):

```powershell
$command = @{
  operation = "create_primitive"
  parameters = @{ primitive_type = "cube"; object_name = "ArenaDemoCube"; location = @(0, 0, 0) }
  timeout = 30
  request_id = [guid]::NewGuid().ToString()
} | ConvertTo-Json -Depth 6
$result = Invoke-RestMethod -Method Post -Uri "$base/v1/integrations/blender/commands" `
  -ContentType "application/json" -Body $command
$result | ConvertTo-Json -Depth 10
```

The structured response includes `job_id`, operation `status`, operation `result`, `error`, and `duplicate_request`. A completed operation returns its connector result; an add-on failure remains a structured `failed` response. A queued-job timeout returns HTTP 504 plus the job ID. Use the same `request_id` and same command to retry safely; a different payload with that ID returns HTTP 409. The controller persists a retry record before enqueue in a `controller_requests` directory alongside the existing mailbox, and never silently enqueues a second copy of the same identified action. If a retry record survives but all mailbox state has disappeared, it returns an ambiguity error rather than risking a replay; inspect Blender before deliberately resubmitting with a new ID.

Request a **camera render** from Blender's active camera/current render engine, then fetch the PNG bytes directly from the controller:

```powershell
$render = @{
  operation = "capture_screenshot"
  parameters = @{ width = 640; height = 480 }
  timeout = 120
  request_id = [guid]::NewGuid().ToString()
} | ConvertTo-Json -Depth 6
$renderResult = Invoke-RestMethod -Method Post -Uri "$base/v1/integrations/blender/commands" `
  -ContentType "application/json" -Body $render
$renderResult.result
Invoke-WebRequest -Uri ($base + $renderResult.result.screenshot_url) `
  -OutFile (Join-Path $PWD "blender-camera-render.png")
```

This PNG is a **Blender camera render**, not a screenshot of Blender's UI or viewport; UI screenshots remain the separate `/v1/screenshot` endpoint and are Windows-only. The render needs an active camera and may use the current scene/render-engine settings. The adapter confines image reads to the job's mailbox screenshot directory, checks the PNG signature/hash, and caps downloads at 20 MiB. It serves the file directly; Google Drive is not involved.

## Screenshot benchmark

The benchmark accepts a representative screenshot file and optionally a previous screenshot:

```powershell
python benchmarks/screenshot_benchmark.py desktop.png --previous desktop-before.png --link-mbps 100
python benchmarks/screenshot_benchmark.py --capture --link-mbps 100
```

It compares MSS and Pillow ImageGrab capture time when run with `--capture`, then compares PNG, JPEG, WebP, and AVIF (when the local Pillow build has an encoder), raw RGB, base64 transport forms, and compact per-pixel hexadecimal text. It reports encoded bytes, encode/decode time, reconstruction error, a rough text-token estimate, and estimated transfer time at the supplied link rate. Back-to-back capture accuracy comparisons are approximate if the screen changes during the run. With `--previous`, it also measures an exact changed-bounds PNG patch and verifies reconstruction. The estimate is not a real network transfer; the harness cannot measure AI model processing time or actual provider token billing without a configured provider. Keep benchmark outputs local; screenshots can contain sensitive information.

## Tests

The controller and adapter tests run on any Python 3.10+ host without installing Windows extras:

```bash
python -m unittest discover -s tests -v
```

The Blender adapter tests use an independent fake connector/mailbox. They cover allowlist errors, structured add-on failures, bounded timeouts, idempotent retries, screenshot path/integrity checks, and the direct PNG HTTP route. They do not run Blender or the Windows add-on. The current sandbox is Linux, so Windows UI Automation, mouse/keyboard dispatch, live application detection, and physical screenshots cannot be exercised here.

### Live Windows / Blender 5.2 verification

The copy-pasteable, two-terminal procedure—including Python/Windows dependency setup, process detection, `hello_test`, primitive creation and dimension changes, camera rendering, direct PNG retrieval, success checks, and common errors—is in [`docs/windows-blender-controller-test.md`](docs/windows-blender-controller-test.md). It has not been run on a live Windows/Blender machine by this agent; the owner must perform that test before treating the integration as live-verified.
