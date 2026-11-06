# Architecture

## Phase 3: registered local Studio session (preserved)

The Studio plugin initiates connections to the loopback service. After the user supplies `CONNECTOR_TOKEN`, **Connect / refresh overview** calls authenticated `POST /v1/session/register`. The service returns a cryptographically random UUID. The plugin sends an authenticated heartbeat every five seconds to `POST /v1/session/heartbeat`, carrying an overview it reads directly from Studio. It fetches that snapshot through `GET /v1/project/overview` for display.

The overview is bounded: place/universe IDs, `IsStudio`, and class plus direct-child count for a fixed nine-service allowlist. No recursive traversal, script source, player state, OS files, or credentials are included. The server tracks one active session in memory; a newer registration replaces the old one and a heartbeat expires after 15 seconds.

## Phase 4B: browser extension handoff (implemented; `create_test_folder` Windows smoke test succeeded)

```text
Arena Direct prompt
  → user enables handoff in the extension panel
  → extension visibly appends a command-format instruction to each outgoing prompt
  → isolated content script watches only new assistant response code blocks
  → service worker re-validates a single typed operation
  → authenticated loopback API queues a short-lived request
  → Studio plugin displays it and waits for an explicit Approve / Reject click
  → after approval the plugin creates one Workspace Folder or bounded Part
  → plugin reports outcome; extension shows it and can insert an unsent Arena draft
```

The extension lives in `extension/` and is Manifest V3. It has permissions only for the Arena Direct/conversation page patterns, the connector at `127.0.0.1:4317`, and extension-local storage. The token is read only by the extension service worker from `chrome.storage.local`; the content script and Arena page never receive it. The instruction appended to the user's prompt contains only the public command schema, not a credential. Content-to-worker messages are validated again in the worker, which accepts messages only from the expected Arena Direct URLs.

Arena output must contain exactly one `BEGIN_ARENA_ROBLOX_COMMAND` / `END_ARENA_ROBLOX_COMMAND` block inside a rendered `<pre>`, with an exact operation-specific schema (no extra keys). Only rendered `<pre>` elements that are not hidden/ARIA-hidden and intersect the viewport are considered. Repeated references to the same DOM element are counted once; nested elements are collapsed only when they parse to the identical typed command. Distinct visible command blocks and nested conflicts are rejected. A response/render signature must remain unchanged for 1.8 seconds after generation ends before parsing. Only `create_test_folder` and `create_part` are allowed. Text outside the code block, unrelated assistant text, conversation history, and other page data are not sent to the connector. If Direct mode or the expected response structure cannot be confirmed, the extension fails closed and pauses handoff.

The extension request uses a UUID, an 8 KiB server body cap, a 60-second approval expiry, bounded queue/retention, duplicate rejection, and a separate `CONNECTOR_EXTENSION_TOKEN`. Plugin-only heartbeat/decision/result routes use `CONNECTOR_TOKEN`. The server issues a per-operation approval capability in the heartbeat response; the browser extension never receives it. Every mutation requires the Studio plugin's explicit user decision. Approval authorizes one server-validated operation and does not enable arbitrary code execution. The plugin creates one `Folder` or one strictly specified `Part` directly under `Workspace`, with a request-ID attribute so a retry can recognize its own prior result rather than create another object. It rejects any existing child with the requested name; it never overwrites or renames.

The result is returned to the extension status panel. The user may click **Draft result in Arena composer** to place the status in an unsent draft; the extension never auto-sends an assistant-visible claim. The result/queue is memory-only and can be lost if the local server restarts.

## Phase 5A: constrained Part building block

The extension and server accept one additional typed operation, `create_part`. Its exact JSON fields are `operation`, `name`, `position`, `size`, `color`, `material`, `anchored`, and `canCollide` (plus server `requestId`). `name` is 1–40 characters, trimmed, and limited to ASCII letters, digits, spaces, `_`, and `-`. `position` is `{x,y,z}` with finite numeric components in `[-10000,10000]` studs. `size` is `{x,y,z}` with finite numeric components in `[0.1,512]` studs. `color` is `{x,y,z}` of integer RGB components `[0,255]`. `material` must be one of `Plastic`, `SmoothPlastic`, `Wood`, `WoodPlanks`, `Concrete`, `Brick`, `Metal`, `Grass`, `Ice`, `Glass`, `Neon`, `Sand`, `Slate`, or `Marble`. `anchored` and `canCollide` must be booleans. Extra/missing fields are rejected; no rotation, parent, class selection, scripts, edits, deletes, or multi-instance operations are supported. The plugin approval preview presents the exact values and fixed Workspace parent. Existing names cause a safe failure without overwrite or rename. Automated parser/server tests cover schema validation, queueing, expiry, and capability boundaries. The duplicate-name branch and actual Part creation require a Windows Studio integration retest because the plugin runs only inside Roblox Studio.

## Endpoints

- `GET /health`: public process health only.
- `GET /v1/status`: `CONNECTOR_TOKEN`; live plugin session status.
- `POST /v1/session/register`: `CONNECTOR_TOKEN`; creates/replaces the active session.
- `POST /v1/session/heartbeat`: `CONNECTOR_TOKEN`; strict overview plus one pending/previously approved operation for Studio UI.
- `GET /v1/project/overview`: `CONNECTOR_TOKEN`; latest overview from the live session.
- `POST /v1/operations`: `CONNECTOR_EXTENSION_TOKEN`; exact typed `create_test_folder` or `create_part` request only.
- `GET /v1/extension/status`, `GET /v1/operations/:id`: extension token; minimal status/result, never the approval capability.
- `POST /v1/operations/:id/decision` and `/result`: `CONNECTOR_TOKEN` and the per-request approval capability; used only by the Studio plugin.

The default listener remains bound to `127.0.0.1`; no remote relay or public listener is introduced. The server uses extension-origin CORS only for valid `chrome-extension://` origins and still requires bearer authentication on every operational route.

## Arena detection evidence and limitations

The implementation's initial adapter is based on the public ZeroScript Free `providers/arena.js` and README. That provider documents Arena Direct (`/text/direct` and `/c/<id>`), a reverse-ordered `ol.flex-col-reverse`, assistant `.prose` bodies, user-turn `justify-end`, an Arena stop-generation button, and carousel handling; it reports live validation in June 2026. ZeroScript itself supports Direct mode only. It additionally has send mechanics, DOM-repair code, stream heuristics, and broader behavior which this prototype does not copy.

This environment did not expose a live Arena page or browser. The current Arena DOM, exact selector stability, browser local-network permission prompt, and current stream-completion timing are not independently verified for October 2026. The prototype limits itself to those observed selectors, uses a 1.8-second stable-text window plus the stop button, starts tracking only after handoff is enabled, and fails closed when it cannot confirm Direct mode. Real-browser verification is required before relying on it.

## Why retain the custom Studio plugin instead of built-in Studio MCP

Roblox Studio has an official local stdio MCP server. ZeroScript's bridge launches it, but its advertised tools include broad editing and Luau execution. For this first mutation, the existing plugin is easier to constrain to one fixed `Folder` creation, one explicit approval UI, and a clear audit/result path. No Studio MCP server, arbitrary MCP tool dispatch, or ZeroScript code was added.

## Failure and persistence

Plugin heartbeat cadence is five seconds; server staleness is 15 seconds. Each operation expires after 60 seconds and the in-memory request records are bounded. Server restart clears session/command state. A failed plugin result report may be retried while the same Studio session and approved request are live; the request-ID attribute prevents creating a duplicate Folder. A server or Studio restart during a request still requires manual verification/recovery and is not presented as durable job processing.
