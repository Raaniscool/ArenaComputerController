# ArenaComputerController

A small, local-first Windows desktop-control prototype. It exposes a provider-neutral JSON API for desktop state, Windows UI Automation inputs, actions, and screenshots requested on demand.

## Repository assessment

At the start of this work the checkout contained only this README and the initial commit. **Neither `ArenaBlenderWorkshop` nor `ArenaRobloxConnector` has been added to this repository yet.** There is no existing controller, connector source, setup guide, or license text to reuse or inspect. I have therefore left both integrations untouched rather than duplicating or relicensing code. The controller reports Blender/Roblox process detection separately from a real integration connection.

The shortest useful path available in this checkout is a thin Windows API with native window/process discovery, optional UI Automation and screenshot dependencies, stale-input protection, action results, and a reproducible image-encoding benchmark. The first end-to-end Blender workflow remains blocked on the Blender connector files being added.

## Prototype status and scope

Implemented in this first pass:

- Starts a local HTTP API and prints a startup report based on the host's detected processes/windows.
- On Windows, enumerates visible top-level windows, identifies the foreground window, and lists processes without reading command-line arguments or application documents.
- Discovers actionable UI Automation controls through `pywinauto` when installed.
- Assigns revision-bound input IDs. An action against an old revision or disappeared target is rejected instead of being sent to a possibly different control.
- Supports UIA click/focus/type/clear/key actions, window focus/key actions, and coordinate click/scroll fallback actions.
- Returns action success/error, a basic window/application state delta, and a bounded action history. Typed text and key sequences are deliberately not stored in history.
- Captures a full desktop, visible window bounds, or a crop only when `/v1/screenshot` is requested. PNG, JPEG, and WebP are supported when Pillow's encoder is available; scale-down is supported.
- Keeps the API and core tests dependency-free. Windows automation/image libraries are optional extras.

Not yet implemented: the imported Blender/Roblox adapters, application-specific verification, event-driven live updates, automatic input-map diffing, multi-provider AI adapters, a dashboard, and AI-provider latency/token measurements. This is a working foundation, not a claim that those later milestones are complete.

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

On non-Windows hosts the service can still be started and tested, but it reports process-only discovery; window/input actions and screenshots are Windows-only in this prototype.

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

## Screenshot benchmark

The benchmark accepts a representative screenshot file and optionally a previous screenshot:

```powershell
python benchmarks/screenshot_benchmark.py desktop.png --previous desktop-before.png --link-mbps 100
python benchmarks/screenshot_benchmark.py --capture --link-mbps 100
```

It compares MSS and Pillow ImageGrab capture time when run with `--capture`, then compares PNG, JPEG, WebP, and AVIF (when the local Pillow build has an encoder), raw RGB, base64 transport forms, and compact per-pixel hexadecimal text. It reports encoded bytes, encode/decode time, reconstruction error, a rough text-token estimate, and estimated transfer time at the supplied link rate. Back-to-back capture accuracy comparisons are approximate if the screen changes during the run. With `--previous`, it also measures an exact changed-bounds PNG patch and verifies reconstruction. The estimate is not a real network transfer; the harness cannot measure AI model processing time or actual provider token billing without a configured provider. Keep benchmark outputs local; screenshots can contain sensitive information.

## Tests

The API/controller tests run on any Python 3.10+ host without installing Windows extras:

```bash
python -m unittest discover -s tests -v
```

The current sandbox is Linux, so Windows UI Automation, mouse/keyboard dispatch, and physical screenshot capture cannot be exercised here. Those require a real Windows desktop session. Tests use a fake backend only for controller/API contracts; they do not substitute for that Windows validation.
