"""Deterministic, single-operation Supabase polling companion."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from .capture import CaptureSafetyError, CapturedPng
from .protocol import (
    ProtocolError,
    build_initial_snapshot_payload,
    build_result_payload,
    normalize_uuid,
    parse_claim_response,
    parse_timestamp,
)
from .supabase_rpc import SupabaseRpcError


class Gate2Companion:
    """Poll, validate, set one test marker color, and publish its screenshot.

    The class has no mouse, keyboard, shell, filesystem, process-launch, or
    general controller action interface. The only executor method is the
    injected test window's ``set_marker_color`` callback.
    """

    def __init__(
        self,
        client: Any,
        session_id: str,
        window: Any,
        capture: Any,
        *,
        poll_interval_seconds: float = 0.5,
        wait: Callable[[float], bool] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        if not 0.2 <= poll_interval_seconds <= 5.0:
            raise ValueError("Polling interval must be between 0.2 and 5 seconds.")
        self.client = client
        self.session_id = normalize_uuid(session_id, "session_id")
        self.window = window
        self.capture = capture
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen_command_ids: set[str] = set()
        self._seen_lock = threading.Lock()
        self._logger = logger or print
        self._wait = wait or self._stop.wait

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Gate2CompanionPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _log(self, message: str) -> None:
        try:
            self._logger(message)
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        try:
            self.window.set_status(text)
        except Exception:
            pass

    def _run(self) -> None:
        try:
            self._publish_initial_snapshot()
        except CaptureSafetyError:
            self._set_status("Blocked: keep the small test window visible and unobscured")
            self._log("Gate 2 stopped: safe test-window capture could not be guaranteed.")
            return
        except SupabaseRpcError as exc:
            self._set_status("Not connected: check local configuration and the disposable test branch")
            self._log(f"Gate 2 stopped during startup (HTTP {exc.http_status or 'network'}).")
            return
        except Exception:
            self._set_status("Not connected: startup failed; check local configuration")
            self._log("Gate 2 stopped during startup after a local validation error.")
            return

        self._set_status("Connected · waiting for an approved color command")
        self._log(f"Gate 2 companion connected to session {self.session_id[:8]}…")
        while not self._stop.is_set():
            try:
                self.run_once()
            except SupabaseRpcError as exc:
                if not exc.retryable:
                    self._set_status("Session rejected or RPC unavailable; companion stopped")
                    self._log(f"Gate 2 stopped (HTTP {exc.http_status or 'non-retryable'}).")
                    return
                self._set_status("Connection interrupted; retrying")
                self._log("Gate 2 connection interrupted; retrying without replaying claimed commands.")
            except Exception:
                self._set_status("Companion error; waiting for the next poll")
                self._log("Gate 2 poll failed safely; no unrestricted action was attempted.")
            self._wait(self.poll_interval_seconds)

    def _publish_initial_snapshot(self) -> None:
        ping = self.client.ping()
        self._validate_ping(ping)
        self._set_status("Connected · checking test-window visibility")
        screenshot: CapturedPng = self.capture.capture_png()
        payload = build_initial_snapshot_payload(str(uuid4()), screenshot.data, screenshot.width, screenshot.height)
        response = self.client.publish_initial_snapshot(payload)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ProtocolError("Initial screenshot was not accepted.")
        self._log("Initial test-window screenshot published (pixels only; no color metadata).")

    def _validate_ping(self, payload: Any) -> None:
        if not isinstance(payload, dict) or set(payload) != {"ok", "session_id", "session_expires_at", "companion_last_poll_at"}:
            raise ProtocolError("Ping response has an unexpected shape.")
        if payload.get("ok") is not True:
            raise ProtocolError("Test session was not accepted.")
        response_session = normalize_uuid(payload.get("session_id"), "session_id")
        if response_session != self.session_id:
            raise ProtocolError("Ping response belongs to another test session.")
        expires_at = parse_timestamp(payload.get("session_expires_at"), "session_expires_at")
        if expires_at <= datetime.now(timezone.utc):
            raise ProtocolError("Paired test session has expired.")

    def run_once(self) -> str:
        """Poll and execute at most one command; convenient for deterministic tests."""
        response = self.client.claim_next_command()
        command = parse_claim_response(response, self.session_id)
        if command is None:
            return "idle"
        with self._seen_lock:
            if command.command_id in self._seen_command_ids:
                self._log(f"Duplicate claimed command {command.command_id[:8]}… ignored.")
                return "duplicate"
            # Mark before touching the window. A lost response must never re-execute it.
            self._seen_command_ids.add(command.command_id)

        self._set_status("Executing one approved test-window change")
        try:
            if command.operation != "set_marker_color":
                raise ProtocolError("Operation is not allowlisted.")
            self.window.set_marker_color(command.color)
        except Exception:
            published = self._publish_failure(command.command_id, "failed", "executor_failed")
            if published:
                self._set_status("Command failed; no further desktop action is available")
                return "failed"
            self._set_status("Command failed; failure status could not be reported")
            return "report_failed"

        try:
            screenshot: CapturedPng = self.capture.capture_png()
        except CaptureSafetyError:
            published = self._publish_failure(command.command_id, "executed_unverified", "capture_unsafe")
            if published:
                self._set_status("Color action ran, but screenshot safety was not confirmed")
                return "executed_unverified"
            self._set_status("Color action ran; its unverified result could not be reported")
            return "report_failed"
        except Exception:
            published = self._publish_failure(command.command_id, "executed_unverified", "capture_failed")
            if published:
                self._set_status("Color action ran, but screenshot capture failed")
                return "executed_unverified"
            self._set_status("Color action ran; its unverified result could not be reported")
            return "report_failed"

        try:
            payload = build_result_payload(
                command.command_id,
                "succeeded",
                png=screenshot.data,
                width=screenshot.width,
                height=screenshot.height,
            )
        except ProtocolError:
            published = self._publish_failure(command.command_id, "executed_unverified", "screenshot_too_large")
            if published:
                self._set_status("Color action ran, but its screenshot was not publishable")
                return "executed_unverified"
            self._set_status("Color action ran; its unverified result could not be reported")
            return "report_failed"

        try:
            response = self.client.publish_result(payload)
        except SupabaseRpcError:
            self._set_status("Color action ran; result upload failed and will not be replayed")
            self._log(f"Command {command.command_id[:8]}… ran, but its result upload failed; it will not be replayed.")
            return "report_failed"
        if not isinstance(response, dict) or response.get("ok") is not True:
            self._set_status("Color action ran; result was not accepted")
            self._log(f"Command {command.command_id[:8]}… ran, but Supabase did not accept its result.")
            return "report_failed"

        self._set_status("Connected · command completed and screenshot published")
        self._log(f"Command {command.command_id[:8]}… completed; screenshot published.")
        return "succeeded"

    def _publish_failure(self, command_id: str, status: str, error_code: str) -> bool:
        payload = build_result_payload(command_id, status, error_code=error_code)
        try:
            response = self.client.publish_result(payload)
        except Exception:
            self._log(f"Command {command_id[:8]}… could not publish its failure status.")
            return False
        if not isinstance(response, dict) or response.get("ok") is not True:
            self._log(f"Command {command_id[:8]}… failure status was not accepted.")
            return False
        return True
