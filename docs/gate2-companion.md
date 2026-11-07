# Gate 2 Windows companion — preparation only

## Status and boundary

This is preparation code for a single disposable Windows test window. It is not a universal desktop controller and is not a proof of the Windows↔Supabase↔Arena loop. No Supabase branch, table, key, user, policy, function, or row was created or changed while preparing these files. The migration below is local and must not be applied to `main`.

The companion has one action: `set_marker_color` against its own Tk window. It has no listener and no mouse, keyboard, shell, process-launch, file-operation, or general controller action interface. Arena remains the only reasoning component; the Windows code is deterministic.

## Credential security assessment

### Decision

A pairing capability can be used for this narrowly scoped test without a Supabase Auth user, but it is **not a Supabase Auth identity**. Supabase sees the publishable key and routes the request as `anon`; the RPC additionally requires a per-session 256-bit random capability in the `x-arena-gate2-token` HTTP header. The RPC derives the session from the token hash and does not trust a client-supplied session ID.

Supabase/PostgREST exposes request headers to SQL through `current_setting('request.headers', true)`. Supabase also documents that `SECURITY DEFINER` functions run with their owner's privileges, recommends a fixed empty `search_path`, and requires explicit EXECUTE grants. The migration follows those rules. See [Securing your API](https://supabase.com/docs/guides/api/securing-your-api), [Row Level Security](https://supabase.com/docs/guides/database/postgres/row-level-security), and [Database Functions](https://supabase.com/docs/guides/database/functions).

### Why the client uses narrow RPCs instead of table RLS alone

RLS can consult a custom request header, but direct table CRUD would make state transitions and one-time claiming harder to reason about. The prepared migration keeps backing tables in the unexposed `gate2_private` schema, revokes direct client privileges, enables RLS, and exposes only four fixed RPCs to `anon`:

- `gate2_ping`: authenticate the pairing and report its own session/last-poll status.
- `gate2_claim_command`: atomically claims at most one unexpired command with `FOR UPDATE SKIP LOCKED`.
- `gate2_publish_initial_snapshot`: writes a bounded startup image.
- `gate2_publish_result`: records one result for a command claimed by the same session.

The RPCs are `SECURITY DEFINER`, so their owner can bypass RLS. That is an explicit, important trust boundary—not a reason to rely on RLS inside those functions. Their bodies use a fixed `search_path`, schema-qualified objects, no dynamic SQL, narrow arguments, token-to-session checks, and explicit EXECUTE grants. A migration-time `DO` block safely formats the installed `pgcrypto` schema name for the private digest helper. The backing tables are not exposed to the Windows API role. The Arena SQL connector remains privileged and is trusted to operate only on the authorized disposable branch.

### Threat properties and residual questions

- **Stolen database row:** only a SHA-256 hash of a 256-bit CSPRNG token is stored. A row or database snapshot containing the hash does not feasibly reveal the token. The session ID is an identifier, not a credential.
- **Session impersonation:** the RPC finds the session by the token hash. It does not accept a session ID in the request. A result command ID must belong to that token-derived session. Supplying another session's command ID is rejected.
- **Replay:** command IDs and request IDs are UUIDs. The database changes `pending` to `claimed` in one transaction; a claimed command cannot be claimed a second time. Results are unique by command ID and cannot overwrite an earlier result. If execution succeeds but upload fails, the companion does not retry the action; that command remains claimed/unverified and requires a new command.
- **Expiry/revocation:** sessions expire after at most two hours; commands expire within 30 seconds. `pair` rotates to a new session and prints SQL that revokes the prior one before inserting the new hash. `unpair` deletes the local protected token and prints a server-side revocation statement. A stolen live token remains usable until expiry/revocation, but only for the allowlisted session operations.
- **At rest / logging:** the raw token is protected with Windows DPAPI in the current user's local application-data directory. DPAPI user scope protects against casual file disclosure, not malware or another process running as the same Windows user. The token is never printed, placed in a URL/body, or included in companion logs. Only its non-secret hash is printed once so it can be registered through the Arena SQL connector. Supabase documents an allowlist of headers in API Gateway logs; the custom `x-arena-gate2-token` is not on that list and should be omitted from those logs ([Log sources and fields](https://supabase.com/docs/guides/observability/log-field-reference)). This is a useful control, not proof about every project log path: the token is intentionally visible to the Postgres request-header GUC, and custom SQL/audit logging or future code that records all request headers could expose it. Before first pairing, confirm project logging settings and log drains do not capture the header/GUC; do not enable full-header logging. The companion uses the Data API, not Edge Functions (whose invocation logs can include request/response headers; see [Edge Function logging](https://supabase.com/docs/guides/functions/logging)). The publishable key is public by design; `sb_secret_*` and legacy `service_role` keys are rejected.
- **Data exposure:** screenshots are small PNG base64 strings in the private schema. Result rows contain no marker-color text. The initial color exists only in the test app's process and is revealed through image pixels.
- **Remaining verification:** the custom header path, installed `pgcrypto` schema, grants, and function behavior must still be exercised against the one authorized disposable branch. Supabase's docs establish the request-header mechanism, but the live project has not been changed or tested for this migration. Project/database administrators can read the hash and all test rows; the test branch is not a defense against a compromised Supabase owner account. Do not reuse this custom token scheme as a general user-authentication system.

### API-key compatibility

Use a `sb_publishable_*` key in the `apikey` header. New publishable keys are not JWTs and must not be sent as `Authorization: Bearer` tokens. A legacy `anon` JWT is accepted for compatibility and is sent as both `apikey` and `Authorization: Bearer <anon-key>`. The config loader rejects secret/service-role keys. Supabase's [API key guide](https://supabase.com/docs/guides/getting-started/api-keys) documents these differences.

## Prepared files

- `src/arena_computer_controller/gate2/`: local config/DPAPI, protocol validation, fixed Supabase RPC client, Tk test window, fail-closed screenshot wrapper, companion poller, and CLI.
- `supabase/migrations/20261106000100_gate2_windows_companion.sql`: test-only schema, token-hash helpers, bounded tables, and four RPCs. **Not applied.**
- `supabase/tests/gate2_companion.test.sql`: pgTAP checks for direct table denial, function grants, session binding, and one-time claiming. **Not run against PostgreSQL.**
- `scripts/setup_gate2_windows.ps1` and `scripts/run_gate2_windows.ps1`: minimal user-side setup and configure/pair/status/run/unpair commands.

## Setup when a Windows session is available

The sandbox cannot access `C:\Users\Raani\Documents\ArenaComputerController`. The Windows owner must sync this branch into that checkout and run the scripts in an interactive Windows session. Do not copy or paste the program source by hand.

```powershell
.\scripts\setup_gate2_windows.ps1
.\scripts\run_gate2_windows.ps1 -Configure
.\scripts\run_gate2_windows.ps1 -Pair
```

`-Configure` prompts for the project URL and publishable (or legacy anon) key and saves them under `%LOCALAPPDATA%\ArenaComputerController\gate2\config.json`. `-Pair` generates a fresh token, DPAPI-protects it locally, and prints a session ID, token hash, and short bootstrap SQL. **The raw token is never shown.** The hash is not an authentication secret; use the printed SQL only in the authorized disposable branch after its migration is applied. Do not apply it to `main`.

After the branch has the local migration and the pairing row, launch the test:

```powershell
.\scripts\run_gate2_windows.ps1
```

The window is a fixed-size, opaque, borderless, always-on-top 500×440 Tk window with a randomized central square and fixed color buttons. Close the window or press Ctrl+C in its console to stop; both stop polling. The companion will publish an initial screenshot before it polls commands. If capture safety cannot be confirmed, it will stop rather than capture the desktop.

Check pairing connectivity and the last companion poll with:

```powershell
.\scripts\run_gate2_windows.ps1 -Status
```

To remove the local token and revoke the branch session:

```powershell
.\scripts\run_gate2_windows.ps1 -Unpair
```

Run the printed revocation SQL through the SQL connector on the disposable branch. The session also expires automatically. No local listener is opened.

## Screenshot safety

Before creating Tk, the CLI requires Windows per-monitor-v2 DPI awareness so Win32 window bounds and MSS screen pixels use the same coordinate space; if that cannot be enabled, launch fails closed. `WindowsWindowCapture` starts from the Tk root HWND and passes its exact bounds to `SystemDesktopBackend.capture_screenshot(region=...)`. It deliberately avoids `SystemDesktopBackend.get_state()` and the `window_id=...` capture path: that general-purpose lookup enumerates unrelated window titles and process names, outside this test's privacy scope. The region path is a visible-desktop crop; its MSS-first `ImageGrab` fallback also receives the exact bounding box and is never a full-desktop fallback. The wrapper verifies only its own process/window title/bounds, checks it is visible, topmost, and not minimized, and checks overlapping visible windows above it using HWNDs and rectangles without reading their titles. It repeats the target/occlusion checks after the crop and discards the image if the window moved or became obscured. Dimensions must match the fixed 500×440 window exactly. If the region capture fails, the window is clipped, or any check fails, the companion refuses to upload. Capture and Win32 Z-order checks are not atomic, so there is a small residual race if another topmost overlay appears and disappears between checks; use a clean interactive Windows session and do not use this design if that residual is unacceptable.

No initial or resulting marker color is written as text to logs, file names, or result metadata. The actual screenshot pixels are the visual evidence. The UI exposes the set of possible color buttons, but not the current marker state.

## Arena-side SQL shape for the later experiment

After the branch migration and pairing bootstrap are applied, Arena can create a single command with fresh UUIDs and a short TTL:

```sql
INSERT INTO gate2_private.commands
  (command_id, session_id, request_id, operation, color, issued_at, expires_at)
VALUES
  ('<command-uuid>'::uuid, '<session-uuid>'::uuid, '<request-uuid>'::uuid,
   'set_marker_color', 'blue', pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp() + interval '20 seconds')
RETURNING command_id, request_id, expires_at;
```

Read the latest startup pixels from `gate2_private.initial_snapshots`, and a command result from `gate2_private.results`. Retrieve only `screenshot_base64` along with status/timestamp/error code; do not use command color as evidence of the current visible state. Reconstruct the returned base64 to a workspace PNG and open it with `read_file` before deciding the next color. The companion's result has no color field.

These SQL examples and the migration are prepared only. No Supabase branch, command, screenshot, or cloud row was created in this preparation stage.

## Local validation and limits

- The 40 Gate 2 unit tests use fakes/mocks; the full Python suite passed 230 tests (6 optional skips), and the existing Node suite passed 31 tests.
- Python compilation and `python -m arena_computer_controller.gate2 --help` passed.
- `pglast` parsed the migration and pgTAP file, all seven top-level PL/pgSQL function bodies, the migration's `DO` block, and the generated digest/bootstrap SQL. This is syntax parsing, **not** execution against PostgreSQL; the pgTAP test was not run.
- No Supabase resource was created or changed. The migration remains unapplied, and project-specific Postgres/audit/log-drain settings were not inspected.
- No Windows/PowerShell/DPAPI/Tk/MSS execution occurred. This environment cannot access `C:\Users\Raani\Documents\ArenaComputerController`; no companion launch, command, screenshot, or visual feedback is claimed. A real test still needs an interactive Windows session and an authorized disposable Supabase branch.
