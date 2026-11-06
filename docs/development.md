# Development and Windows verification

Requirements: Node.js 18+, Git, Chrome/Edge, and Roblox Studio. There are no npm dependencies. The server listens on `127.0.0.1:4317` only.

## Update the existing Windows checkout safely

```powershell
Set-Location (Join-Path $HOME 'Documents\ArenaRobloxConnector')
git status --short --branch
git branch --show-current
```

Review any local changes before updating; do not use reset/checkout to discard them. When the worktree is clean, fast-forward only the Arena branch:

```powershell
git pull --ff-only origin arena/73fd4f32-arenarobloxconnecter
npm test
```

## Configure and start the loopback service

`CONNECTOR_TOKEN` remains the existing Studio-plugin secret. Keep its value unchanged if it is already stored in Studio. Create a separate extension secret once, persist it only in the current Windows user's environment, and copy it into the extension popup without printing it:

```powershell
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$env:CONNECTOR_EXTENSION_TOKEN = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
[Environment]::SetEnvironmentVariable('CONNECTOR_EXTENSION_TOKEN', $env:CONNECTOR_EXTENSION_TOKEN, 'User')
Set-Clipboard $env:CONNECTOR_EXTENSION_TOKEN
```

In a new PowerShell session, load both environment values from your own secure setup; the extension token can be read for process startup with `$env:CONNECTOR_EXTENSION_TOKEN = [Environment]::GetEnvironmentVariable('CONNECTOR_EXTENSION_TOKEN', 'User')`. Set `CONNECTOR_TOKEN` to the same existing plugin secret. The two tokens must differ and each must be at least 32 characters.

Then start the server and keep the window open:

```powershell
if ($env:CONNECTOR_TOKEN.Length -lt 32) { throw 'Set the existing Studio CONNECTOR_TOKEN first.' }
if ($env:CONNECTOR_EXTENSION_TOKEN.Length -lt 32) { throw 'Set CONNECTOR_EXTENSION_TOKEN first.' }
$env:PORT = '4317'
npm start
```

Do not print, commit, or paste either secret into Arena. The Studio token is entered only in the Studio plugin settings; the extension token is entered only in the extension popup. Extension storage is local to the browser profile and is not an encrypted vault. To rotate it, update the Windows environment, restart the connector, and replace the token in the extension popup.

## Update/install the Studio plugin

Open `studio-plugin/ArenaRobloxConnector.plugin.lua` and replace the complete source of the existing local plugin, then **Plugins → Save as Local Plugin**. Open the **Arena Connector** widget, enter the matching `CONNECTOR_TOKEN`, and click **Connect / refresh overview**. Confirm the session and read-only overview are live. The widget should also show that every requested operation needs explicit approval.

When a command is pending, the plugin widget shows the Folder name and fixed target `Workspace`, with **Approve Folder** and **Reject** buttons. Approve only a request you intended. Rejecting or letting it expire must not create an instance.

## Load the Manifest V3 extension

1. Open `chrome://extensions` (Chrome) or `edge://extensions` (Edge) and enable Developer mode.
2. Choose **Load unpacked** and select the repository's `extension` folder.
3. Open the extension popup and paste the clipboard value created above into **Extension token**, then **Save token**. Use the popup's **Check connector** button to verify loopback reachability and Studio status.
4. Navigate to Arena Direct (`https://arena.ai/text/direct`) and confirm the extension panel says Arena Direct is active. Turn on **Enable Arena Direct handoff**. No handoff is active by default.
5. The extension visibly appends its public command-format instruction to each outgoing prompt while enabled. Review the appended text in the composer; it contains no secret. Do not enable it in Battle, Side-by-Side, Agent, Search, or other modes.

If Chrome/Edge asks for access to the Arena site or the local connector, grant only the listed Arena and `127.0.0.1:4317` access needed by the extension. If the extension says Direct mode or the expected composer/response structure cannot be confirmed, stop and report the observed UI—do not add speculative selectors or use network interception.

## Manual end-to-end checklist (not run in this Linux workspace)

The live Arena page and Windows Studio are not available to the coding agent. Do this test locally before relying on the prototype:

1. Run `npm test`; confirm the Node server and Studio plugin are connected.
2. In Arena Direct, enable the handoff. Ask for a normal answer that does not request a Studio change. Confirm no operation is queued.
3. Ask for one test Folder named `ArenaSmoke` under Workspace. Confirm the assistant's structured command appears in a code block and the extension reports it as queued.
4. Before approving, inspect `Workspace` in Studio and confirm no new Folder exists. Click **Reject**; confirm the extension reports rejection and no Folder appears.
5. Ask again with a new request ID. Confirm the pending name and target in Studio; click **Approve Folder** once. Verify exactly one `Folder` named `ArenaSmoke` appears under `Workspace`, and that the result is reported back to the extension.
6. Optionally click **Draft result in Arena composer**; review the unsent draft before sending it. The extension never sends an assistant follow-up automatically.
7. Stop the connector or Studio plugin and confirm the request expires/disconnects safely. Restarting the Node process clears in-memory session/operation state.

If the Arena response is not detected reliably, the page mode is ambiguous, or a command appears outside a rendered code block, stop testing and report the exact observed structure. Do not use Battle/Agent mode or relax the parser. `create_test_folder` has passed a Windows end-to-end smoke test; `create_part` remains unverified until the real browser/Studio checklist passes.


## Phase 5A create_part manual verification

After updating the unpacked extension and replacing/reloading the Studio plugin, verify only with a throwaway/basic request: ask Arena Direct to create one anchored `SmoothPlastic` Part named `ArenaPlatform` at `(0, 5, 0)`, size `(12, 1, 12)`, RGB `(80, 160, 220)`, with `CanCollide` enabled. Confirm the Studio approval panel shows those exact properties and `Workspace`; reject once and verify no Part appears. Then send a fresh request, inspect the same preview, approve, and verify exactly one Part with those properties appears under `Workspace`. Finally submit a request whose name already exists and confirm it fails without overwriting or renaming. Do not test scripts, arbitrary instance types, or other operations. Record the observed result; local tests do not prove this Studio path.

## Plugin-side name guard and test limitation

The plugin checks pending names before calling `setApproval` (and before the approved-operation recovery path): nonempty string, maximum 40 characters, no leading/trailing whitespace, and only ASCII letters, digits, spaces, `_`, or `-`. It rejects rather than normalizes invalid names. The Node suite cannot execute Roblox Luau or the Studio plugin runtime, and the normal server will reject invalid names before they reach Studio; there is no supported route for injecting an invalid heartbeat payload in the production flow. Thus this plugin guard has static code review, not an automated runtime test.

Manual Studio checklist after updating the local plugin:

1. Open the plugin in Studio and use Script Analysis to check for Luau diagnostics; load the widget and confirm the preview and controls render.
2. Submit and approve a valid Folder request with a name such as `Arena Test_1-OK`; verify the name is preserved exactly and the Folder is created once.
3. Submit a valid Part request with a valid name; verify its full preview and approve it.
4. Confirm the plugin source’s pending-operation gate calls `validOperationName` before either displaying a request or recovering an approved one. Do not weaken the production server to inject malformed requests. Testing rejection of malformed heartbeat data at runtime would require a separate, isolated test harness and is not covered by this prototype.
