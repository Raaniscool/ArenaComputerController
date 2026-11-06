# Testing record

Updated: 2026-11-05

## Existing controller, Blender, and MCP coverage

Before the independent agent runtime was added, the controller/bridge suite was recorded at 171 tests. The stdio MCP adapter and existing controller/Blender integration remain unchanged by the runtime work.

- `PYTHONPATH=src:. python -m unittest discover -s tests -q` — 171 tests passed; 6 optional MCP SDK tests were skipped in the default environment.
- The same pre-runtime suite in a disposable Python 3.11 environment with `mcp==2.3.0` — 171 tests passed, no skips. MCP coverage includes six-tool registration, operation enum matching the existing `SUPPORTED_OPERATIONS`, stable `request_id`, native JPEG/PNG image content, and stdio initialize/list-tools plus image-result round trip.
- `npm test` — 31 Roblox connector tests passed.
- `python -m compileall -q src tests`, `git diff --check`, TOML parsing, and `python -m pip install --dry-run --no-deps -e '.[mcp]'` — passed for that earlier adapter revision.

Resolved earlier MCP diagnostic: an initial SDK registration check exposed deferred return annotations that the SDK could not resolve on nested tool functions. The annotations were removed; the optional-SDK suite and stdio tests passed afterward.

## Independent agent runtime (current implementation)

- `PYTHONPATH=src:. python -m unittest tests.test_agent_cli tests.test_agent_provider tests.test_agent_runtime -v` — 19 agent tests passed.
- `PYTHONPATH=src:. python -m unittest discover -s tests -q` — 190 total tests passed, with 6 optional MCP SDK tests skipped in the default Python environment.
- The same full suite using the existing disposable environment with `mcp==2.3.0` — 190 tests passed, no skips.
- `npm test` — 31 tests passed.
- `PYTHONPATH=src:. python -m compileall -q src tests` — passed.
- `python -m arena_computer_controller.agent --help` — passed without requiring an API key or controller connection.
- `git diff --check` and `pyproject.toml` parsing/entry-point validation — passed.
- The first `pip install --dry-run` attempt against the system Python was blocked by the OS's PEP 668 externally-managed-environment guard. No system install was attempted. Retrying in a disposable venv with `--dry-run --no-deps --no-build-isolation -e .` passed and reported the editable project as installable.

Coverage added for the agent:

- OpenAI-compatible tool/function schema serialization and response parsing.
- Actual screenshot bytes serialized as a `data:image/png;base64,...` image input (not a file path or URL).
- API URL restrictions, timeouts/request-response size limits, invalid responses, and safe HTTP-error messages that do not echo the API key or response body.
- `get_computer_state` followed by `get_screenshot`, image bytes attached to the next model turn, tool error dispatch, and multiple tool-reply ordering.
- CLI loopback `/health` preflight before any model call (so an offline controller fails without using model quota), plus CLI budget parsing.
- Blender approval denied by default, explicit approval flow, stable request ID derived from the tool-call ID, tool-call budget, and preserved six-tool surface.

These are unit tests with fake model responses and a fake controller bridge. They do **not** prove a real Arena Router API call, real provider tool calling, real Windows screenshot capture, or model visual understanding. No API key or screenshot data was supplied to the test run.

## Owner-reported Windows/Blender results (not independently run here)

On 2026-11-05 the owner reported a Windows 11 local run at `http://127.0.0.1:8765`: the controller started, Blender was detected, UI Automation discovered 106 interactive inputs, `GET /health` returned `ok: true`, and `GET /v1/screenshot` returned HTTP 200 with `image/png`; a 403,510-byte screenshot was retrieved and opened. Blender mailbox configuration was detected. The owner had also previously reported a successful Blender 5.2.0 LTS probe/command/object/render/image-retrieval run. These are owner-reported results, not sandbox verification. They verify the controller and screenshot retrieval, but not the independent agent's model call or model visual interpretation.

## Outstanding / not verified

- Arena Router API eligibility, live tool-call behavior, current model support, exact account pricing, and image input on the user's workspace.
- On Windows, verify that the runtime calls `get_computer_state`, reads its result, calls `get_screenshot`, sends the image bytes in the next model request, and describes visible pixels.
- On Windows with Blender 5.2, run `blender_probe`, create one harmless named primitive with console approval, retrieve a camera render, and confirm actual model vision.
- General Windows action tools, app launch, persistent session/UI behavior, provider retry/backoff, and Roblox integration are future work.
- Physical Windows UI Automation and screenshots are not exercised in this Linux sandbox.

Record any owner-side run below, without secrets or sensitive screenshot contents. Include provider/model, image format/size, tool sequence, outcome, and cost if the provider dashboard reports it.

### Owner-side agent acceptance results

_Not run yet._

## Failures

No outstanding test failures. The PEP 668 packaging diagnostic was resolved by performing the editable-install dry run in a disposable venv; no global packages were changed. The expected fixture log `Job missing-object-job failed: object_not_found` appears during the existing Python suite and is not a test failure.
