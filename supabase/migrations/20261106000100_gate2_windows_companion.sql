-- Gate 2 Windows companion: LOCAL PREPARATION ONLY.
-- Do not apply this file to main or to any branch until the owner authorizes
-- exactly one disposable test branch. The Windows client never receives a
-- service-role/secret key.

CREATE SCHEMA IF NOT EXISTS gate2_private AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA gate2_private FROM PUBLIC, anon, authenticated, service_role;

-- Install pgcrypto only if needed. The token digest helper below locates the
-- extension's actual schema rather than assuming it is public or extensions.
CREATE SCHEMA IF NOT EXISTS extensions AUTHORIZATION postgres;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

CREATE TABLE gate2_private.sessions (
    session_id uuid PRIMARY KEY,
    token_hash bytea NOT NULL UNIQUE CHECK (pg_catalog.octet_length(token_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.transaction_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    companion_last_poll_at timestamptz,
    CHECK (expires_at > created_at),
    CHECK (expires_at <= created_at + interval '2 hours')
);

CREATE TABLE gate2_private.commands (
    command_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES gate2_private.sessions(session_id),
    request_id uuid NOT NULL UNIQUE,
    operation text NOT NULL CHECK (operation = 'set_marker_color'),
    color text NOT NULL CHECK (color IN ('red', 'green', 'blue', 'yellow', 'purple')),
    issued_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    expires_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'succeeded', 'failed', 'executed_unverified', 'expired')),
    claimed_at timestamptz,
    finished_at timestamptz,
    CHECK (expires_at > issued_at),
    CHECK (expires_at <= issued_at + interval '30 seconds')
);
CREATE INDEX gate2_commands_pending_idx
    ON gate2_private.commands (session_id, issued_at, command_id)
    WHERE status = 'pending';

CREATE TABLE gate2_private.initial_snapshots (
    snapshot_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES gate2_private.sessions(session_id),
    captured_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    width integer NOT NULL CHECK (width BETWEEN 1 AND 960),
    height integer NOT NULL CHECK (height BETWEEN 1 AND 720),
    screenshot_base64 text NOT NULL
        CHECK (pg_catalog.length(screenshot_base64) BETWEEN 1 AND 240000),
    CHECK (width::bigint * height::bigint <= 691200)
);
CREATE INDEX gate2_initial_snapshots_session_idx
    ON gate2_private.initial_snapshots (session_id, captured_at DESC);

CREATE TABLE gate2_private.results (
    command_id uuid PRIMARY KEY REFERENCES gate2_private.commands(command_id),
    session_id uuid NOT NULL REFERENCES gate2_private.sessions(session_id),
    status text NOT NULL
        CHECK (status IN ('succeeded', 'failed', 'executed_unverified')),
    completed_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    width integer,
    height integer,
    screenshot_base64 text,
    error_code text,
    CHECK (
        (screenshot_base64 IS NULL AND width IS NULL AND height IS NULL)
        OR
        (screenshot_base64 IS NOT NULL
         AND width IS NOT NULL AND height IS NOT NULL
         AND pg_catalog.length(screenshot_base64) BETWEEN 1 AND 240000
         AND width BETWEEN 1 AND 960
         AND height BETWEEN 1 AND 720
         AND width::bigint * height::bigint <= 691200)
    ),
    CHECK (
        (status = 'succeeded' AND screenshot_base64 IS NOT NULL AND error_code IS NULL)
        OR
        (status <> 'succeeded' AND error_code IS NOT NULL)
    ),
    CHECK (
        error_code IS NULL OR error_code IN (
            'executor_failed', 'capture_unsafe', 'capture_failed', 'screenshot_too_large'
        )
    )
);
CREATE INDEX gate2_results_session_idx
    ON gate2_private.results (session_id, completed_at DESC);

-- The private tables are not available through the Data API. RLS is enabled as
-- a second barrier if this schema is accidentally exposed in a later revision.
-- Table-owner bypass is intentional: SECURITY DEFINER RPCs run as the owner
-- and must access the rows. Direct client grants remain revoked; keep these
-- function bodies as small, fixed-SQL security boundaries.
ALTER TABLE gate2_private.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE gate2_private.commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE gate2_private.initial_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE gate2_private.results ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES IN SCHEMA gate2_private FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA gate2_private FROM PUBLIC, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA gate2_private
    REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA gate2_private
    REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated, service_role;

DO $gate2_extension_setup$
DECLARE
    extension_schema text;
BEGIN
    SELECT namespace.nspname
      INTO extension_schema
      FROM pg_catalog.pg_extension AS extension
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = extension.extnamespace
     WHERE extension.extname = 'pgcrypto';

    IF extension_schema IS NULL THEN
        RAISE EXCEPTION 'pgcrypto must be available for the Gate 2 token hash';
    END IF;

    EXECUTE pg_catalog.format($sql$
        CREATE OR REPLACE FUNCTION gate2_private._token_digest(p_token text)
        RETURNS bytea
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = ''
        AS $function$
            SELECT %I.digest(
                pg_catalog.convert_to(%L || p_token, 'UTF8'),
                'sha256'
            )
        $function$
    $sql$, extension_schema, 'ArenaComputerController/Gate2/Pairing/v1:');
END;
$gate2_extension_setup$;

CREATE OR REPLACE FUNCTION gate2_private._constant_time_equal(p_left bytea, p_right bytea)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path = ''
AS $function$
DECLARE
    position integer;
    difference integer := 0;
BEGIN
    IF pg_catalog.octet_length(p_left) <> 32 OR pg_catalog.octet_length(p_right) <> 32 THEN
        RETURN false;
    END IF;
    FOR position IN 0..31 LOOP
        difference := difference | (pg_catalog.get_byte(p_left, position) # pg_catalog.get_byte(p_right, position));
    END LOOP;
    RETURN difference = 0;
END;
$function$;

-- Reads the custom HTTP header populated by PostgREST and derives the session
-- from its hash. No caller-supplied session_id is trusted.
CREATE OR REPLACE FUNCTION gate2_private._session_for_request()
RETURNS TABLE (session_id uuid, session_expires_at timestamptz, companion_last_poll_at timestamptz)
LANGUAGE plpgsql
VOLATILE
SET search_path = ''
AS $function$
DECLARE
    request_headers jsonb;
    pairing_token text;
    matching_header_count integer;
    candidate_hash bytea;
    matched_session_id uuid;
    matched_expiry timestamptz;
    matched_last_poll timestamptz;
BEGIN
    BEGIN
        request_headers := COALESCE(
            pg_catalog.current_setting('request.headers', true)::jsonb,
            '{}'::jsonb
        );
        SELECT pg_catalog.count(*)::integer, pg_catalog.min(header.value)
          INTO matching_header_count, pairing_token
          FROM pg_catalog.jsonb_each_text(request_headers) AS header(key, value)
         WHERE pg_catalog.lower(header.key) = 'x-arena-gate2-token';
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Gate 2 pairing rejected';
    END;

    IF matching_header_count <> 1
       OR pairing_token IS NULL
       OR pg_catalog.length(pairing_token) <> 43
       OR pairing_token !~ '^[A-Za-z0-9_-]{43}$' THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Gate 2 pairing rejected';
    END IF;

    candidate_hash := gate2_private._token_digest(pairing_token);
    SELECT session_row.session_id, session_row.expires_at, session_row.companion_last_poll_at
      INTO matched_session_id, matched_expiry, matched_last_poll
      FROM gate2_private.sessions AS session_row
     WHERE gate2_private._constant_time_equal(session_row.token_hash, candidate_hash)
       AND session_row.revoked_at IS NULL
       AND session_row.expires_at > pg_catalog.clock_timestamp()
     LIMIT 1
     FOR UPDATE OF session_row;

    IF matched_session_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Gate 2 pairing rejected';
    END IF;

    RETURN QUERY SELECT matched_session_id, matched_expiry, matched_last_poll;
END;
$function$;

CREATE OR REPLACE FUNCTION gate2_private._validate_png(
    p_screenshot_base64 text,
    p_width integer,
    p_height integer
)
RETURNS bytea
LANGUAGE plpgsql
IMMUTABLE
SET search_path = ''
AS $function$
DECLARE
    png_bytes bytea;
    png_width bigint;
    png_height bigint;
BEGIN
    IF p_screenshot_base64 IS NULL
       OR pg_catalog.length(p_screenshot_base64) < 1
       OR pg_catalog.length(p_screenshot_base64) > 240000
       OR p_width IS NULL
       OR p_height IS NULL
       OR p_width < 1 OR p_width > 960
       OR p_height < 1 OR p_height > 720
       OR p_width::bigint * p_height::bigint > 691200 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Gate 2 screenshot';
    END IF;

    BEGIN
        png_bytes := pg_catalog.decode(p_screenshot_base64, 'base64');
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Gate 2 screenshot';
    END;

    IF pg_catalog.octet_length(png_bytes) < 33
       OR pg_catalog.octet_length(png_bytes) > 180000
       OR pg_catalog.substring(png_bytes, 1, 8) <> pg_catalog.decode('89504e470d0a1a0a', 'hex')
       OR pg_catalog.substring(png_bytes, 13, 4) <> pg_catalog.convert_to('IHDR', 'UTF8') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Gate 2 screenshot';
    END IF;

    png_width := pg_catalog.get_byte(png_bytes, 16)::bigint * 16777216
               + pg_catalog.get_byte(png_bytes, 17)::bigint * 65536
               + pg_catalog.get_byte(png_bytes, 18)::bigint * 256
               + pg_catalog.get_byte(png_bytes, 19)::bigint;
    png_height := pg_catalog.get_byte(png_bytes, 20)::bigint * 16777216
                + pg_catalog.get_byte(png_bytes, 21)::bigint * 65536
                + pg_catalog.get_byte(png_bytes, 22)::bigint * 256
                + pg_catalog.get_byte(png_bytes, 23)::bigint;

    IF png_width <> p_width OR png_height <> p_height THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Gate 2 screenshot';
    END IF;
    RETURN png_bytes;
END;
$function$;

CREATE OR REPLACE FUNCTION public.gate2_ping()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    paired_session record;
BEGIN
    SELECT session_row.session_id, session_row.session_expires_at, session_row.companion_last_poll_at
      INTO paired_session
      FROM gate2_private._session_for_request() AS session_row;
    RETURN pg_catalog.jsonb_build_object(
        'ok', true,
        'session_id', paired_session.session_id,
        'session_expires_at', paired_session.session_expires_at,
        'companion_last_poll_at', paired_session.companion_last_poll_at
    );
END;
$function$;

CREATE OR REPLACE FUNCTION public.gate2_claim_command()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    paired_session record;
    selected_command gate2_private.commands%ROWTYPE;
    claimed_command jsonb := NULL;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    SELECT session_row.session_id, session_row.session_expires_at
      INTO paired_session
      FROM gate2_private._session_for_request() AS session_row;

    UPDATE gate2_private.sessions AS session_row
       SET companion_last_poll_at = v_now
     WHERE session_row.session_id = paired_session.session_id
       AND session_row.revoked_at IS NULL
       AND session_row.expires_at > v_now;

    UPDATE gate2_private.commands AS command
       SET status = 'expired', finished_at = v_now
     WHERE command.session_id = paired_session.session_id
       AND command.status = 'pending'
       AND command.expires_at <= v_now;

    SELECT command.*
      INTO selected_command
      FROM gate2_private.commands AS command
     WHERE command.session_id = paired_session.session_id
       AND command.status = 'pending'
       AND command.issued_at <= v_now + interval '5 seconds'
       AND command.expires_at > v_now
       AND command.expires_at <= v_now + interval '30 seconds'
     ORDER BY command.issued_at, command.command_id
     LIMIT 1
     FOR UPDATE SKIP LOCKED;

    IF FOUND THEN
        UPDATE gate2_private.commands AS command
           SET status = 'claimed', claimed_at = v_now
         WHERE command.command_id = selected_command.command_id
           AND command.status = 'pending'
        RETURNING command.* INTO selected_command;

        IF FOUND THEN
            claimed_command := pg_catalog.jsonb_build_object(
                'command_id', selected_command.command_id,
                'request_id', selected_command.request_id,
                'session_id', selected_command.session_id,
                'operation', selected_command.operation,
                'color', selected_command.color,
                'issued_at', selected_command.issued_at,
                'expires_at', selected_command.expires_at,
                'status', selected_command.status
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'ok', true,
        'session_id', paired_session.session_id,
        'session_expires_at', paired_session.session_expires_at,
        'command', claimed_command
    );
END;
$function$;

CREATE OR REPLACE FUNCTION public.gate2_publish_initial_snapshot(
    p_snapshot_id uuid,
    p_screenshot_base64 text,
    p_width integer,
    p_height integer
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    paired_session record;
    inserted_rows integer;
    snapshot_count integer;
BEGIN
    SELECT session_row.session_id, session_row.session_expires_at
      INTO paired_session
      FROM gate2_private._session_for_request() AS session_row;

    PERFORM gate2_private._validate_png(p_screenshot_base64, p_width, p_height);

    SELECT pg_catalog.count(*)::integer
      INTO snapshot_count
      FROM gate2_private.initial_snapshots AS snapshot
     WHERE snapshot.session_id = paired_session.session_id
       AND snapshot.snapshot_id <> p_snapshot_id
       AND snapshot.captured_at > pg_catalog.clock_timestamp() - interval '2 hours';
    IF snapshot_count >= 4 THEN
        RAISE EXCEPTION USING ERRCODE = '54000', MESSAGE = 'Gate 2 snapshot limit reached';
    END IF;

    INSERT INTO gate2_private.initial_snapshots (
        snapshot_id, session_id, width, height, screenshot_base64
    ) VALUES (
        p_snapshot_id, paired_session.session_id, p_width, p_height, p_screenshot_base64
    ) ON CONFLICT (snapshot_id) DO NOTHING;
    GET DIAGNOSTICS inserted_rows = ROW_COUNT;
    IF inserted_rows = 0 AND NOT EXISTS (
        SELECT 1
          FROM gate2_private.initial_snapshots AS existing_snapshot
         WHERE existing_snapshot.snapshot_id = p_snapshot_id
           AND existing_snapshot.session_id = paired_session.session_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Gate 2 snapshot rejected';
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'ok', true,
        'duplicate', inserted_rows = 0,
        'snapshot_id', p_snapshot_id
    );
END;
$function$;

CREATE OR REPLACE FUNCTION public.gate2_publish_result(
    p_command_id uuid,
    p_status text,
    p_screenshot_base64 text,
    p_width integer,
    p_height integer,
    p_error_code text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    paired_session record;
    selected_command gate2_private.commands%ROWTYPE;
BEGIN
    SELECT session_row.session_id, session_row.session_expires_at
      INTO paired_session
      FROM gate2_private._session_for_request() AS session_row;

    IF p_status IS NULL OR p_status NOT IN ('succeeded', 'failed', 'executed_unverified') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Gate 2 result';
    END IF;
    IF p_error_code IS NOT NULL AND p_error_code NOT IN (
        'executor_failed', 'capture_unsafe', 'capture_failed', 'screenshot_too_large'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Gate 2 result';
    END IF;
    IF (p_status = 'succeeded' AND (p_error_code IS NOT NULL OR p_screenshot_base64 IS NULL))
       OR (p_status <> 'succeeded' AND p_error_code IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Gate 2 result';
    END IF;

    IF p_screenshot_base64 IS NULL THEN
        IF p_width IS NOT NULL OR p_height IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Gate 2 result';
        END IF;
    ELSE
        PERFORM gate2_private._validate_png(p_screenshot_base64, p_width, p_height);
    END IF;

    SELECT command.*
      INTO selected_command
      FROM gate2_private.commands AS command
     WHERE command.command_id = p_command_id
       AND command.session_id = paired_session.session_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Gate 2 result rejected';
    END IF;

    IF EXISTS (
        SELECT 1 FROM gate2_private.results AS result
         WHERE result.command_id = p_command_id
           AND result.session_id = paired_session.session_id
    ) THEN
        RETURN pg_catalog.jsonb_build_object('ok', true, 'duplicate', true);
    END IF;

    IF selected_command.status <> 'claimed'
       OR selected_command.expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Gate 2 result rejected';
    END IF;

    UPDATE gate2_private.commands AS command
       SET status = p_status,
           finished_at = pg_catalog.clock_timestamp()
     WHERE command.command_id = p_command_id
       AND command.session_id = paired_session.session_id
       AND command.status = 'claimed';

    INSERT INTO gate2_private.results (
        command_id, session_id, status, width, height, screenshot_base64, error_code
    ) VALUES (
        p_command_id, paired_session.session_id, p_status,
        p_width, p_height, p_screenshot_base64, p_error_code
    );

    RETURN pg_catalog.jsonb_build_object('ok', true, 'duplicate', false);
END;
$function$;

-- No API client can read or mutate backing tables directly. Only these four
-- narrowly scoped RPCs are available to anon; each requires the per-session
-- token in x-arena-gate2-token and derives the session server-side.
REVOKE ALL ON FUNCTION gate2_private._token_digest(text) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION gate2_private._constant_time_equal(bytea, bytea) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION gate2_private._session_for_request() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION gate2_private._validate_png(text, integer, integer) FROM PUBLIC, anon, authenticated, service_role;

REVOKE ALL ON FUNCTION public.gate2_ping() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.gate2_claim_command() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.gate2_publish_initial_snapshot(uuid, text, integer, integer) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.gate2_publish_result(uuid, text, text, integer, integer, text) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.gate2_ping() TO anon;
GRANT EXECUTE ON FUNCTION public.gate2_claim_command() TO anon;
GRANT EXECUTE ON FUNCTION public.gate2_publish_initial_snapshot(uuid, text, integer, integer) TO anon;
GRANT EXECUTE ON FUNCTION public.gate2_publish_result(uuid, text, text, integer, integer, text) TO anon;

COMMENT ON SCHEMA gate2_private IS 'Gate 2 test-only queue and screenshots; never expose to the Data API.';
COMMENT ON TABLE gate2_private.sessions IS 'Stores only a hash of a high-entropy pairing token; raw tokens remain on the Windows user profile.';
COMMENT ON TABLE gate2_private.commands IS 'One-session allowlisted Gate 2 commands; never expose directly to the Data API.';
COMMENT ON TABLE gate2_private.initial_snapshots IS 'Small startup screenshots; no text field stores the marker color.';
COMMENT ON TABLE gate2_private.results IS 'Command status and screenshot pixels; no marker-color result metadata.';
