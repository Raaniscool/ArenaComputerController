# Testing record

Updated: 2026-11-05

## Verified before this MCP implementation

These results were recorded at commit `1442233d4a97e40105ecf3da3e2855da995925a5` before the current MCP adapter changes:

- `python -m unittest discover -s tests -v` — 150 tests passed.
- `npm test` — 31 tests passed.
- `python -m compileall -q src tests` — passed.
- `git diff --check` — passed.
- The Windows/Blender guide and existing controller integration had prior simulated test coverage.

## Current implementation tests

- `PYTHONPATH=src:. python -m unittest discover -s tests -q` — 171 tests passed; 6 optional MCP SDK tests skipped because the default environment does not have the SDK installed.
- The same full suite in a disposable Python 3.11 environment with `mcp==2.3.0` — 171 tests passed, no skips. MCP-specific coverage includes six-tool registration, operation enum matching the existing `SUPPORTED_OPERATIONS`, required stable `request_id`, native JPEG/PNG image content, and stdio initialize/list-tools plus a complete stdio image-result round trip.
- `npm test` — 31 tests passed.
- `python -m compileall -q src tests`, `git diff --check`, parsing `pyproject.toml`, and `python -m pip install --dry-run --no-deps -e '.[mcp]'` — passed.
- The MCP SDK was installed only in `/tmp/arena-mcp-test-venv`; no runtime dependency was installed into the project environment.

Resolved implementation diagnostic: the first MCP SDK registration check exposed deferred return annotations that the SDK could not resolve on nested tool functions. The annotations were removed; the complete optional-SDK suite and stdio tests now pass.

## Owner-reported Windows/Blender results (not independently run here)

The owner reported a successful run using Windows, Blender 5.2.0 LTS, and the controller endpoint `http://127.0.0.1:8765`, including controller routes, Blender probe/commands, object creation, EEVEE render, image retrieval, and opening the image. These are user-reported results, not sandbox verification. A two-object, image-informed iteration run through Arena MCP is still outstanding.

## Outstanding / not verified

- Whether Arena's MCP host can spawn the stdio process on the owner's Windows machine.
- Whether a screenshot `ImageContent` result reaches the model's visual context in this Arena session.
- The requested two-object workflow: create two objects, modify one, render, retrieve and visually inspect, apply an image-informed change, render again.
- Complete scene inventory and unreported Blender properties remain unavailable through the existing controller tools.
- Live Windows UI Automation and physical desktop screenshot behavior are not exercised in this Linux sandbox.

## Failures

No outstanding test failures. The resolved SDK diagnostic above is included to preserve the implementation test history.
