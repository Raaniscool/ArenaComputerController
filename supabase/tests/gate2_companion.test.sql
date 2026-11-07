-- pgTAP security/claim checks for the Gate 2 migration.
-- Run only against a local disposable Supabase database, never production.
BEGIN;
SELECT plan(13);

SELECT ok(
    NOT has_schema_privilege('anon', 'gate2_private', 'USAGE'),
    'anon cannot use the private Gate 2 schema'
);
SELECT ok(
    NOT has_table_privilege('anon', 'gate2_private.sessions', 'SELECT'),
    'anon cannot read pairing hashes'
);
SELECT ok(
    NOT has_table_privilege('anon', 'gate2_private.commands', 'SELECT')
    AND NOT has_table_privilege('anon', 'gate2_private.commands', 'INSERT')
    AND NOT has_table_privilege('anon', 'gate2_private.commands', 'UPDATE')
    AND NOT has_table_privilege('anon', 'gate2_private.commands', 'DELETE'),
    'anon cannot access the command table directly'
);
SELECT ok(
    has_function_privilege('anon', 'public.gate2_ping()', 'EXECUTE'),
    'anon can call the token-gated ping RPC'
);
SELECT ok(
    NOT has_function_privilege('authenticated', 'public.gate2_ping()', 'EXECUTE'),
    'authenticated users do not inherit the anonymous companion RPC'
);
SELECT ok(
    (SELECT procedure.prosecdef
       FROM pg_catalog.pg_proc AS procedure
      WHERE procedure.oid = 'public.gate2_ping()'::regprocedure),
    'ping is an explicitly reviewed security-definer RPC'
);

INSERT INTO gate2_private.sessions (session_id, token_hash, expires_at)
VALUES (
    'a0000000-0000-4000-8000-000000000001'::uuid,
    gate2_private._token_digest(repeat('A', 43)),
    pg_catalog.transaction_timestamp() + interval '1 hour'
), (
    'b0000000-0000-4000-8000-000000000002'::uuid,
    gate2_private._token_digest(repeat('B', 43)),
    pg_catalog.transaction_timestamp() + interval '1 hour'
);

INSERT INTO gate2_private.commands (
    command_id, session_id, request_id, operation, color, issued_at, expires_at
) VALUES (
    'c0000000-0000-4000-8000-000000000003'::uuid,
    'a0000000-0000-4000-8000-000000000001'::uuid,
    'd0000000-0000-4000-8000-000000000004'::uuid,
    'set_marker_color', 'blue', pg_catalog.clock_timestamp(),
    pg_catalog.clock_timestamp() + interval '20 seconds'
), (
    'e0000000-0000-4000-8000-000000000005'::uuid,
    'b0000000-0000-4000-8000-000000000002'::uuid,
    'f0000000-0000-4000-8000-000000000006'::uuid,
    'set_marker_color', 'green', pg_catalog.clock_timestamp(),
    pg_catalog.clock_timestamp() + interval '20 seconds'
);

SELECT set_config('request.headers', '{"x-arena-gate2-token":"' || repeat('A', 43) || '"}', true);
SELECT is(
    public.gate2_ping() ->> 'session_id',
    'a0000000-0000-4000-8000-000000000001',
    'the pairing token identifies only its mapped session'
);
SELECT set_config('request.headers', '{"X-Arena-Gate2-Token":"' || repeat('A', 43) || '"}', true);
SELECT is(
    public.gate2_ping() ->> 'session_id',
    'a0000000-0000-4000-8000-000000000001',
    'HTTP header names are matched case-insensitively'
);
SELECT set_config(
    'request.headers',
    '{"x-arena-gate2-token":"' || repeat('A', 43) || '","X-Arena-Gate2-Token":"' || repeat('B', 43) || '"}',
    true
);
SELECT throws_ok(
    $$SELECT public.gate2_ping()$$,
    '42501',
    'Gate 2 pairing rejected',
    'ambiguous duplicate pairing headers are rejected'
);
SELECT set_config('request.headers', '{"x-arena-gate2-token":"' || repeat('Z', 43) || '"}', true);
SELECT throws_ok(
    $$SELECT public.gate2_ping()$$,
    '42501',
    'Gate 2 pairing rejected',
    'a token hash cannot be used as the raw pairing token'
);
SELECT set_config('request.headers', '{"x-arena-gate2-token":"' || repeat('A', 43) || '"}', true);
SELECT is(
    public.gate2_claim_command() -> 'command' ->> 'command_id',
    'c0000000-0000-4000-8000-000000000003',
    'the companion claims only its session command'
);
SELECT is(
    public.gate2_claim_command() -> 'command',
    'null'::jsonb,
    'a claimed command cannot be replayed'
);
SELECT is(
    (SELECT status FROM gate2_private.commands
      WHERE command_id = 'e0000000-0000-4000-8000-000000000005'::uuid),
    'pending',
    'another session command remains untouched'
);
SELECT * FROM finish();
ROLLBACK;
