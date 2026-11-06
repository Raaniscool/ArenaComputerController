# Roadmap

Updated: 2026-11-05

## Product direction

Build ArenaComputerController into an independent computer-control platform with its own reasoning runtime and, later, a simple local interface. Do not require OpenCode, Arena Web Agent Mode, another agent application, or a hosted control listener. Keep the controller bound to loopback. Preserve the existing desktop controller, Blender 5.2 mailbox/add-on integration, bounded Blender operation allowlist, optional stdio MCP adapter, and separate Roblox Studio connector.

## Existing foundation (preserve)

- Local controller exposes state, active window, UI Automation inputs/actions, coordinate fallback actions, on-demand screenshots, and bounded Blender mailbox operations.
- The Blender adapter reuses the existing connector's operation allowlist/schemas, request-ID idempotency, probe, render, and screenshot retrieval; no add-on schema changes are part of the agent-runtime work.
- The optional MCP adapter remains stdio-only and uses the loopback controller. It is an interoperability path, not the required reasoning runtime.
- The repository also contains a separate Node Roblox connector, browser extension, and Studio plugin. They remain intact and are not yet wired into the new agent.
- On 2026-11-05 the owner reported Windows 11 controller verification: `/health` was healthy, UI Automation found 106 controls, and a 403,510-byte PNG screenshot was retrieved and opened. Blender 5.2 integration was previously reported successful. These are owner-reported, not independently rerun in this sandbox.

## Implemented: first independent runtime slice

- Provider-neutral agent messages, tool specifications, events, results, and `ModelProvider` protocol.
- Stdlib OpenAI Chat Completions-compatible adapter, defaulting to the Arena Router endpoint/model. It supports function tool calls, actual image bytes as data-URI image content, request timeouts and size limits, HTTPS (or loopback-only HTTP), disabled redirects, and sanitized provider errors.
- Local CLI (`python -m arena_computer_controller.agent` / `arena-computer-agent`) with environment-based model configuration and no external agent-runtime dependency. It preflights the loopback `/health` route before any model request to avoid spending API quota when the controller is offline.
- Bounded sequential model/tool loop, progress events (including image MIME type/byte count), call/turn limits, and explicit console approval for `blender_execute`.
- Six tools reuse `ControllerMCPBridge`: computer state, active window, desktop screenshot, Blender probe, existing allowlisted Blender execution, and Blender screenshot retrieval.
- Unit tests cover provider request/tool-call parsing, approval gating, tool dispatch/errors, budgets, and screenshot bytes in the subsequent model request.

This slice is source- and unit-test-verified only. No API key was available, no live provider request was made, and this Linux sandbox cannot exercise Windows UI or Blender. Do not mark the first milestone complete until an owner-side run confirms actual model tool calls and visual interpretation of screenshot bytes.

## Next: first live Windows/API milestone

1. On the owner's Windows machine, start the controller on loopback and verify `/health` and `/v1/state`.
2. Configure a provider API key in the local process environment; never commit or share the key. Default provider is Arena Router (`https://api.arena.ai/v1`, `coding-router-preview`), contingent on workspace API eligibility and current availability. Check account usage/pricing before making calls. An OpenAI-compatible provider/model can be selected with the same environment configuration.
3. Run a natural-language screen description. Verify the runtime calls `get_computer_state`, observes its result, then calls `get_screenshot`.
4. Verify the actual PNG/JPEG bytes are serialized as image input on the next provider request. Confirm the response describes visible content, not only metadata/path/URL.
5. Record model name, provider, actual image format/size, outcome, and any cost reported by the provider in `TESTING.md` without recording secrets or sensitive screenshot contents.

## Then: harmless Blender validation

1. Confirm the existing Blender 5.2 add-on responds to `blender_probe`.
2. In a disposable scene, request one uniquely named basic primitive; explicitly approve it in the local CLI.
3. Fetch the camera-render screenshot and verify the model receives and interprets actual returned image bytes.
4. Test a small image-informed follow-up only after the first object/render path works. Keep every operation on the existing allowlist and request-ID/idempotency path.

## Later increments (after live MVP)

1. Expose a minimal set of general computer actions as typed model tools, starting with focus/click/type/key operations. Add explicit confirmation/risk policy, stale-target checks, per-call timeouts, and tests before tool exposure. Do not imply general computer control exists just because the controller has REST endpoints.
2. Add a simple local interface for task entry, progress/tool log, screenshot preview, stop, and approval. Keep local-only and authenticated if the design ever requires a non-loopback bind.
3. Add a native second provider only when useful, with provider-specific tool/image conformance tests; retain the common runtime protocol.
4. Consider connecting the existing Roblox Studio path via a typed local integration, preserving its explicit Studio approval boundary and separate credentials.
5. Benchmark representative JPEG/PNG/WebP sizes and visual quality, scaling/cropping, hashing, and cache behavior before custom image encoding or permanent caching.

## Blockers and security boundaries

- Arena API access is workspace-dependent; a local CLI cannot use Agent Mode's sandbox tools as if they were a custom API. Live key access, tool-call compatibility, image input, model availability, and account pricing remain user-side checks.
- Windows, Blender 5.2, and real desktop screenshots are not attached to this sandbox.
- The first runtime exposes read tools and Blender integration operations only; it does not yet expose desktop action tools, launch applications, or integrate Roblox.
- Keep both model and controller secrets out of source control and logs. Keep all controller endpoints on loopback. Never tunnel or expose an unauthenticated endpoint.
- Treat screenshots and desktop state as sensitive data sent to the configured model provider. Use only providers and workspaces the owner approves.
- Do not add arbitrary Python/shell execution or bypass the Blender allowlist/approval gate.

## Optional MCP path

The existing stdio MCP adapter remains available for external local hosts and is tested separately. It is not a prerequisite to run the Python agent. Its host-launch behavior and delivery of MCP image blocks into a particular model's visual context are still distinct, unverified claims; use the new runtime's direct API image-input acceptance test for the first milestone.
