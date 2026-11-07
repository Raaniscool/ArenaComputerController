"""Small setup/status/launch CLI for the Gate 2 Windows test."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Sequence

from .capture import CaptureSafetyError, WindowsWindowCapture, enable_capture_dpi_awareness
from .companion import Gate2Companion
from .credential_store import (
    ConfigError,
    configure,
    default_config_path,
    load_config,
    load_pairing_token,
    pair,
    unpair_local,
)
from .supabase_rpc import SupabaseRpcClient, SupabaseRpcError
from .window import MarkerTestWindow
from .local_test import LocalTestError, run_local_test


def _config_path(argument: str | None) -> Path:
    return Path(argument).expanduser() if argument else default_config_path()


def _make_client(path: Path) -> tuple[object, object]:
    config = load_config(path)
    client = SupabaseRpcClient(config, load_pairing_token(config))
    return config, client


def _configure(path: Path) -> int:
    print("Gate 2 uses a Supabase publishable key (or legacy anon key), never a secret/service-role key.")
    project_url = input("Supabase project URL: ").strip()
    api_key = getpass.getpass("Supabase publishable/anon key: ")
    configure(path, project_url, api_key)
    print(f"Local configuration saved at: {path}")
    print("No database connection or cloud change was made.")
    return 0


def _pair(path: Path) -> int:
    bootstrap = pair(path)
    print("Pairing is stored locally using Windows DPAPI; the raw token was not printed.")
    print(f"Session ID: {bootstrap.session_id}")
    print(f"Token SHA-256 (non-secret bootstrap value): {bootstrap.token_hash_hex}")
    print("Apply this SQL only to the authorized disposable branch:")
    print(bootstrap.bootstrap_sql)
    print("Never copy, log, or send the raw pairing token; it is not displayed by this command.")
    return 0


def _status(path: Path) -> int:
    config, client = _make_client(path)
    if not config.session_id:
        raise ConfigError("No paired session is configured. Run pair first.")
    result = client.ping()
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise SupabaseRpcError("Supabase did not confirm the paired session.")
    if result.get("session_id") != config.session_id:
        raise SupabaseRpcError("Supabase returned a different session ID.")
    print(f"Connected: yes\nSession: {config.session_id}\nProject: {config.project_url}")
    print(f"Expires: {result.get('session_expires_at', 'unknown')}")
    print(f"Last companion poll: {result.get('companion_last_poll_at') or 'not seen yet'}")
    print("Pairing token and API key were not displayed.")
    return 0


def _unpair(path: Path) -> int:
    revoke_sql = unpair_local(path)
    if revoke_sql is None:
        print("No local pairing was present.")
        return 0
    print("Local DPAPI-protected pairing was removed.")
    print("For immediate server-side revocation, run this SQL only on the disposable branch:")
    print(revoke_sql)
    print("Otherwise the session expires automatically after its short lifetime.")
    return 0


def _run(path: Path) -> int:
    if os.name != "nt":
        raise ConfigError("The Gate 2 window and screenshot companion can run only in an interactive Windows session.")
    try:
        enable_capture_dpi_awareness()
    except CaptureSafetyError:
        raise ConfigError("Per-monitor DPI awareness could not be enabled; safe screenshot capture is unavailable.") from None
    config, client = _make_client(path)
    if not config.session_id:
        raise ConfigError("No paired session is configured. Run pair first.")

    root = None
    try:
        import tkinter as tk

        root = tk.Tk()
    except Exception:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        raise ConfigError("Tkinter could not open the interactive Windows test window.") from None

    companion_holder: list[Gate2Companion] = []

    def request_stop() -> None:
        if companion_holder:
            companion_holder[0].stop()

    try:
        window = MarkerTestWindow(root, on_close=request_stop)
        capture = WindowsWindowCapture(target_handle=window.native_handle)
    except Exception:
        try:
            root.destroy()
        except Exception:
            pass
        raise ConfigError("The test-window capture target could not be initialized safely.") from None
    companion = Gate2Companion(client, config.session_id, window, capture)
    companion_holder.append(companion)
    root.after(250, companion.start)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        request_stop()
    except Exception:
        raise ConfigError("The Gate 2 test window stopped unexpectedly.") from None
    finally:
        companion.stop()
        companion.join(timeout=1.0)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arena-gate2",
        description="A narrow Windows test-window companion; not a general desktop controller.",
    )
    parser.add_argument("--config", help="Optional local config path (default is under %%LOCALAPPDATA%%).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("configure", help="Save project URL and publishable/anon key locally.")
    subparsers.add_parser("pair", help="Generate a protected local pairing token and a non-secret SQL bootstrap.")
    subparsers.add_parser("status", help="Check the current paired test session without revealing credentials.")
    subparsers.add_parser("run", help="Open the isolated test window and start the polling companion.")
    subparsers.add_parser("unpair", help="Erase the local token and print SQL to revoke its test session.")
    subparsers.add_parser(
        "local-test",
        help="Open the local marker window and save one scoped PNG without credentials or Supabase.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "local-test":
            return run_local_test()
        path = _config_path(args.config)
        if args.command == "configure":
            return _configure(path)
        if args.command == "pair":
            return _pair(path)
        if args.command == "status":
            return _status(path)
        if args.command == "run":
            return _run(path)
        if args.command == "unpair":
            return _unpair(path)
        raise ConfigError("Unknown Gate 2 command.")
    except (ConfigError, SupabaseRpcError, LocalTestError) as exc:
        print(f"Gate 2: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Gate 2 stopped by the user.", file=sys.stderr)
        return 130
