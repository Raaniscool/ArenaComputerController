# Roadmap

Updated: 2026-11-05

## Completed foundation

- The existing local controller exposes desktop state, on-demand screenshots, and a bounded Blender mailbox integration.
- The Blender route reuses the connector's existing operation allowlist, schemas, request-ID idempotency, probe, render, and screenshot retrieval; no command schemas were changed for this MCP work.
- The owner reported a successful live Windows/Blender 5.2 run of the controller, Blender probe and operations, camera render, and image retrieval. This is owner-reported, not a run performed by the current sandbox agent.
- MCP interface research selected a small local stdio adapter. Community reports about Arena MCP and image handling are not verification of this user account or platform behavior.

## Implemented and sandbox-verified: local stdio MCP adapter

- Six tools expose controller state, active window, desktop screenshot, Blender probe, existing allowlisted Blender execution, and Blender camera-render retrieval.
- Tool results contain structured JSON metadata and native MCP image content. Image payloads are bounded and byte-identical repeats are suppressed unless explicitly forced.
- Blender operations remain restricted to the existing supported operation names and schemas; MCP requires a stable `request_id` for every operation call.
- The adapter process uses stdio only, calls the controller through a loopback-only HTTP client, and opens no network listener or tunnel.
- Optional SDK packaging, Windows setup guidance, bridge and SDK tests, and a real MCP stdio initialize/list-tools test are in place. See `TESTING.md` for exact results.

## Next: owner-side integration and acceptance run

1. Verify whether the Arena MCP host can launch a local stdio process on the owner's Windows machine. This remains unverified; do not present the adapter as an Arena connection until tested.
2. If local process launch is available, configure the MCP command and verify all six tools are visible.
3. Run the end-to-end modeling acceptance scenario on the user's Blender system:
   - probe the add-on;
   - create two objects;
   - modify one object;
   - render and fetch the camera image;
   - confirm the image reaches the model's visual context and inspect it;
   - make an image-informed change and render again.
4. Record owner-reported Windows/Blender results separately from sandbox results in `TESTING.md`.

## Known gaps and boundaries

- Current controller tools do not provide a complete Blender scene inventory. The model must not infer object names, transforms, materials, or other properties that a tool result did not return.
- MCP image blocks are implemented and tested through the local SDK, but delivery into Arena's model vision context has not been verified.
- If Arena cannot launch this stdio process locally, stop and obtain explicit approval before designing any remote route. Do not expose an unauthenticated controller, add a listener/tunnel, or switch to a network architecture without that approval.
- Preserve the existing Roblox and Blender Workshop scope. Do not add arbitrary Python or shell execution to Blender and do not silently change the controller/add-on operation schemas.
