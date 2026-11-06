# Research notes (updated 2026-10-25)

## Verified user-side Studio result

The user manually tested the Phase 3 plugin on Windows Roblox Studio. The widget reported a live heartbeat and read-only overview, with `Is Studio: true` and actual service child counts. The user also rotated the local token after it appeared in a screenshot. This verifies Windows Studio ↔ loopback connector communication, not Arena → PC invocation.

## Verified Arena Agent Mode facts

- The current Agent Mode documentation says its built-in tools include web search, image generation, file upload, coding assistance, and Bash in a sandbox. It does not describe arbitrary MCP registration, local process execution on the user's computer, a remote MCP client, or persistent outbound sessions from Windows. [Arena Agent Mode help](https://help.arena.ai/articles/5432423882-how-to-use-agent-mode)
- Arena's coding help says a connected repository is copied into a separate sandbox and Agent Mode can manage Git operations such as clone, commit, push, and PR creation. It describes GitHub repository access, not execution on the user's Windows PC or a custom tool endpoint. [Arena coding in Agent Mode help](https://help.arena.ai/articles/1655691990-how-to-use-coding-in-agent-mode)
- In this session, the visible tools include a Linux sandbox Bash, workspace file tools, web search, and local Git/GitHub CLI access. No MCP invocation/registration or remote-tool-call interface is exposed to this agent. I cannot inspect private Arena internals or the user's account settings; public documentation and this session do not prove whether a hidden/account-specific custom-tool feature exists.
- Repo inspection found no `.github/workflows` files, and `gh workflow list` returned no workflows. Git/GitHub access here is repository source/version control; it is not a configured Windows command runner or request relay.

These observations establish that **this session cannot currently invoke the user's localhost connector**. They do not establish that no Arena product or future configuration could support custom tools.

## Architecture comparison

### 1. Browser extension on the user's Arena Direct tab (prototype implemented; live validation pending)

Inspection found the public [ZeroScript Free repository](https://github.com/theo441/zeroscript-free), checked at commit `3307d4e` (2026-07-11). Its README documents an Arena Direct-only browser-extension → loopback bridge → Roblox Studio MCP flow. Its [Arena provider](https://github.com/theo441/zeroscript-free/blob/3307d4e/zeroscript-extension/providers/arena.js) describes live validation in June 2026 and provides specific response mechanics: a reverse-ordered `ol.flex-col-reverse`, direct turn children with `.prose`, user turns marked `justify-end`, a visible Stop-generation button during generation, and special A/B carousel handling. The provider gate excludes Battle, Side-by-Side, and Agent modes. Its [parser](https://github.com/theo441/zeroscript-free/blob/3307d4e/zeroscript-extension/core/parser.js) parses delimited command/tool formats. The bridge uses Roblox Studio's built-in stdio MCP server. This is concrete third-party evidence that Arena Direct responses have been exposed to an extension, not proof that the October 2026 DOM is unchanged or that the selectors are officially supported.

I inspected the provider, parser, MV3 background, README, manifest, and bridge. ZeroScript's full agent loop and broad MCP tool dispatch do not meet this connector's one-operation/approval constraints; no source code was copied. Our smaller extension uses the provider's documented response structure as its initial adapter, requires a rendered `<pre>` and a stable response after the Stop button is gone, starts only after explicit enablement, appends a visible command-format instruction, and fails closed unless it can confirm Direct mode. It sends only a UUID and the parsed typed command to the extension worker/local API—never conversation history. The worker keeps a separate extension token out of the page and the Studio plugin uses a separate credential plus per-request approval capability.

Chrome's extension platform documents service-worker cross-origin requests with host permissions, and content scripts can access the permitted page DOM. [Chrome cross-origin requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests), [content scripts](https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts), [match patterns](https://developer.chrome.com/docs/extensions/develop/concepts/match-patterns). This establishes browser-platform feasibility, not Arena integration stability. No live Arena/Chrome session is exposed here: current selectors, streaming behavior, local-network permission prompts, and end-to-end reliability remain unverified. The extension is a constrained prototype, not a claim of tested Arena response detection.

### 2. Local MCP on Windows

A local stdio MCP server could call `http://127.0.0.1:4317` from the same Windows machine. It would preserve loopback-only networking and avoid a hosted relay. However, an MCP client must run on the same machine as the MCP process. The Agent Mode runtime documented here runs Bash in its sandbox; no supported mechanism to launch a Windows MCP process or register its stdio tools is documented or exposed. This option is therefore not currently verified as usable with this Arena session.

### 3. Windows outbound agent plus authenticated relay

A small Windows agent could maintain an outbound TLS/WebSocket connection to a relay, while Arena calls a remote tool endpoint on that relay. This avoids inbound ports on the user's PC and can work across remote networks **if** Arena supports calling that remote endpoint and the sandbox/network policy permits it. Neither capability has been verified. It would require operating an external service, authentication and key rotation, per-user/session authorization, replay protection, bounded queues/timeouts, availability monitoring, and ongoing hosting/maintenance cost. No relay was deployed and no public endpoint or tunnel was created.

### 4. GitHub mailbox / Actions queue

A Windows poller could theoretically fetch jobs from a private GitHub repository while Arena writes requests there. This is not a genuine direct tool call, adds latency and secret/data retention in GitHub, needs narrowly scoped credentials and a queue protocol, and there is no Actions workflow in this repository. Arena's documented GitHub feature is repository sandboxing/version control, not a guaranteed arbitrary authenticated dispatch interface. Not recommended as the first live bridge.

## Scope decision and remaining blocker

The ZeroScript source is sufficient evidence to implement a narrow Arena Direct prototype without inventing a selector from scratch, but not sufficient to claim reliability today. We therefore implemented only the constrained flow and explicitly leave current Arena DOM/streaming verification to a real browser. The extension fails closed if its Direct-mode and response-shape assumptions do not hold; it does not switch to broad page scraping or Agent/Battle modes. If manual testing shows the assistant response cannot be reliably detected, stop this extension approach rather than adding speculative selectors or network interception.

Arena Agent Mode remains a separate, unverified path: this session cannot call the user's localhost connector. No relay or Agent Mode MCP adapter was implemented. The end-to-end Folder result has not been tested with real Arena and Studio; follow the manual checklist in `docs/development.md` before relying on it. In case the Arena DOM path fails, re-check an Arena-supported custom remote-tool API before considering an operated outbound relay.

## Connector facts

The Node connector remains loopback-only at `127.0.0.1:4317`. Phase 3 overview/status routes use `CONNECTOR_TOKEN`; extension enqueue/status routes use a distinct `CONNECTOR_EXTENSION_TOKEN`. Session and operation state are in memory. The heartbeat is five seconds; session expiry is 15 seconds; operation approval expires in 60 seconds. The read-only overview remains limited to allowlisted service class/direct-child counts plus place/universe IDs and `IsStudio`.

The only mutation is `create_test_folder`, with a bounded name under `Workspace`. The Studio plugin—not the browser extension—requires explicit approval and performs the `Instance.new("Folder")` operation. There is no arbitrary Luau/JavaScript execution, shell, filesystem access, Part creation, relay, or automatic assistant follow-up.


## Later implementation update

The initial Phase 4B scope described above was limited to `create_test_folder`. That operation has since passed a Windows end-to-end smoke test. Phase 5A adds the strictly bounded `create_part` described in [architecture](architecture.md); it has not yet been manually tested in Studio. No general-purpose instance creation or script execution is supported.
