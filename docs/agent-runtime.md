# Independent AI agent runtime

Updated: 2026-11-05

## Current architecture

```text
Natural-language task
  -> AgentRuntime: bounded model/tool loop, progress events, approval callback
  -> ModelProvider interface
       -> OpenAI-compatible Chat Completions adapter (Arena Router default)
  -> six local tool definitions
       -> existing ControllerMCPBridge (reused directly; no MCP SDK required)
  -> http://127.0.0.1:8765
       -> existing Windows controller / Blender mailbox integration
```

The independent runtime is a Python CLI (`python -m arena_computer_controller.agent` or the `arena-computer-agent` entry point). Before its first model request, it checks the local controller's `/health` endpoint, so a controller outage is reported without incurring model usage. It does not require OpenCode, Arena Web Agent Mode, a browser extension, a public endpoint, or an MCP host. The existing optional stdio MCP adapter remains unchanged and available for other local MCP clients.

## Provider choice

The initial provider is a small stdlib-based OpenAI Chat Completions-compatible adapter. The default configuration targets the documented Arena Router endpoint `https://api.arena.ai/v1` and model `coding-router-preview`; it uses a user-created API key read from `ARENA_MODEL_API_KEY`. Arena Router documentation lists tool calling and image input for the OpenCode example, but workspace API access, current model availability, pricing, and this project's direct integration must be verified in the user's account. No API key is included in source or configuration files.

The agent core consumes a provider-neutral `ModelProvider` protocol and normalized message/tool types. A native Anthropic or other provider adapter can be added without changing tool execution. The first provider is not claimed to be live-tested here.

## Tool loop and safety

- A model turn can return text, function calls, or both. The runtime executes registered tools locally and feeds their results back until a final response or configured limit is reached.
- Tool calls are executed sequentially. The CLI reports the returned image's MIME type and byte count when it attaches that payload to the next model request. The default budget is eight model turns and twelve tool calls per task.
- The initial registry reuses the same six operations as the stdio bridge: computer state, active window, desktop screenshot, Blender probe, allowlisted Blender execution, and Blender render screenshot retrieval.
- `blender_execute` is approval-gated by the CLI. Missing, declined, or failed approval means no operation is sent. The existing Blender connector still validates the operation schema and request ID.
- Tool exceptions are converted to safe structured failures; raw exception text and API-key values are not added to model messages.
- The model API key and controller token are separate. The model endpoint must use HTTPS unless it is a local test server. Controller URLs remain loopback-only through the existing bridge.

## Screenshot path

`ControllerMCPBridge` performs on-demand capture, validates MIME/signature and size, attaches metadata and a SHA-256 digest, and suppresses byte-identical repeat payloads unless `force=true`. Its default desktop screenshot is JPEG, quality 90, scale 0.75; PNG/WebP and other quality/scale choices are supported by the tool. Blender render retrieval remains PNG.

When a tool returns image bytes, the runtime preserves the tool's structured metadata and adds a separate image-bearing user message. The OpenAI-compatible adapter encodes those bytes in an `image_url` data URI (`data:image/<type>;base64,...`) in the actual model request. It does not rely on a local path or screenshot URL. Unit tests verify that exact data-URI payload. Only a live model response that correctly describes the image proves visual understanding; that remains an owner-side acceptance test.

No custom image format, patch transfer, or permanent image cache has been introduced. Existing JPEG/PNG/WebP, resolution scaling, byte hash deduplication, and the screenshot benchmark are the initial strategy. Region/window cropping is available through the controller API but is not yet exposed as a tool parameter.

## Current limitations

- The runtime is a one-task CLI, not a persistent session or graphical interface.
- Model requests are non-streaming. Tool-call progress is emitted to stderr; Ctrl+C stops the process, but there is no separate remote cancellation API.
- No model API key was available to this sandbox, Arena's API endpoint could not be called from here, and Windows/Blender are not attached to this environment. Actual provider tool calling, local screenshot capture, and image interpretation must be verified on the Windows PC.
- The agent registry does not yet expose general Windows input/action routes or application launching. The underlying controller has UI Automation and coordinate-action endpoints, but those are not currently model tools. Roblox Studio's existing plugin/server/extension path is separate and has not been wired into this agent.
- No retry/backoff policy for provider API calls or durable conversation storage is implemented.

## First acceptance sequence

1. On Windows, start the existing controller and confirm `/health` and `/v1/state` locally.
2. Configure `ARENA_MODEL_API_KEY` locally, then run a read-only natural-language screen-description task.
3. Confirm model API calls contain tool definitions and that the model calls `get_computer_state`, then `get_screenshot`.
4. Confirm the screenshot's actual bytes are present in the subsequent model input and the answer describes visible content—not just the path or metadata.
5. With Blender 5.2 open, ask for `blender_probe`; only then test a uniquely named basic object creation with the CLI approval prompt.
6. Request and visually inspect the returned camera-render image. Record owner-side results in `TESTING.md` separately from sandbox unit tests.

## Future increments

1. Verify one live Arena Router tool call and one live screenshot-inspection result.
2. Run the harmless Blender probe and one approved primitive creation/render.
3. Add a minimal local interface for task entry, progress/tool log, screenshot preview, stop, and approval.
4. Expose safe UIA/window actions through bounded tool schemas with approval controls; add application launch only after a dedicated allowlist and tests.
5. Add provider adapters and provider-specific image/tool-result conformance tests.
6. Connect the existing Roblox Studio flow through a typed local integration, preserving its separate explicit approval boundary.
7. Benchmark ordinary JPEG/PNG/WebP size and visual quality on representative screens before changing image encoding or caching.
