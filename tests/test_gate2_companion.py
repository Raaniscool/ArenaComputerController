from __future__ import annotations

import struct
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from arena_computer_controller.gate2.capture import CaptureSafetyError, CapturedPng
from arena_computer_controller.gate2.companion import Gate2Companion
from arena_computer_controller.gate2.protocol import build_result_payload
from arena_computer_controller.gate2.supabase_rpc import SupabaseRpcError


def make_png(width: int = 32, height: int = 24, rgb: tuple[int, int, int] = (50, 100, 200)) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(bytes([0]) + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    signature = bytes.fromhex("89504e470d0a1a0a")
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class FakeWindow:
    def __init__(self, fail=False):
        self.colors = []
        self.statuses = []
        self.fail = fail

    def set_marker_color(self, color):
        if self.fail:
            raise RuntimeError("internal error must not leak")
        self.colors.append(color)

    def set_status(self, status):
        self.statuses.append(status)


class FakeCapture:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def capture_png(self):
        self.calls += 1
        if self.fail:
            raise CaptureSafetyError("not safe")
        return CapturedPng(make_png(32, 24), 32, 24)


class FakeClient:
    def __init__(self, session_id, command=None, *, publish_error=False):
        self.session_id = session_id
        self.command = command
        self.results = []
        self.snapshots = []
        self.publish_error = publish_error
        self.claim_calls = 0

    def ping(self):
        return {
            "ok": True,
            "session_id": self.session_id,
            "session_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "companion_last_poll_at": None,
        }

    def claim_next_command(self):
        self.claim_calls += 1
        return {
            "ok": True,
            "session_id": self.session_id,
            "session_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "command": self.command,
        }

    def publish_initial_snapshot(self, payload):
        self.snapshots.append(payload)
        return {"ok": True, "duplicate": False}

    def publish_result(self, payload):
        self.results.append(payload)
        if self.publish_error:
            raise SupabaseRpcError("offline", retryable=True)
        return {"ok": True, "duplicate": False}


def make_command(session_id, *, color="blue", operation="set_marker_color", expires_in=20):
    now = datetime.now(timezone.utc)
    return {
        "command_id": str(uuid4()),
        "request_id": str(uuid4()),
        "session_id": session_id,
        "operation": operation,
        "color": color,
        "issued_at": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
        "status": "claimed",
    }


class Gate2CompanionTests(unittest.TestCase):
    def setUp(self):
        self.session_id = str(uuid4())

    def companion(self, command=None, *, capture=None, window=None, client=None):
        client = client or FakeClient(self.session_id, command)
        window = window or FakeWindow()
        capture = capture or FakeCapture()
        companion = Gate2Companion(client, self.session_id, window, capture, logger=lambda _: None)
        return companion, client, window, capture

    def test_initial_snapshot_is_sent_as_pixels_without_color_metadata(self):
        companion, client, _, _ = self.companion()
        companion._publish_initial_snapshot()
        self.assertEqual(len(client.snapshots), 1)
        payload = client.snapshots[0]
        self.assertIn("p_screenshot_base64", payload)
        self.assertNotIn("color", payload)
        self.assertNotIn("marker_color", payload)
        self.assertNotIn("observed_color", payload)

    def test_allowlisted_command_executes_once_and_result_has_no_color_field(self):
        command = make_command(self.session_id, color="green")
        companion, client, window, capture = self.companion(command)
        self.assertEqual(companion.run_once(), "succeeded")
        self.assertEqual(window.colors, ["green"])
        self.assertEqual(capture.calls, 1)
        self.assertEqual(len(client.results), 1)
        result = client.results[0]
        self.assertEqual(result["p_command_id"], command["command_id"])
        self.assertEqual(result["p_status"], "succeeded")
        self.assertNotIn("color", result)
        self.assertNotIn("marker_color", result)
        self.assertNotIn("observed_color", result)
        self.assertNotIn("green", repr(result))
        self.assertEqual(companion.run_once(), "duplicate")
        self.assertEqual(window.colors, ["green"])
        self.assertEqual(len(client.results), 1)

    def test_wrong_session_and_unsupported_action_never_reach_window(self):
        command = make_command(str(uuid4()))
        companion, _, window, _ = self.companion(command)
        with self.assertRaises(ValueError):
            companion.run_once()
        self.assertEqual(window.colors, [])

        command = make_command(self.session_id, operation="press_key")
        companion, _, window, _ = self.companion(command)
        with self.assertRaises(ValueError):
            companion.run_once()
        self.assertEqual(window.colors, [])

    def test_expired_command_never_reaches_window(self):
        command = make_command(self.session_id, expires_in=-2)
        companion, _, window, _ = self.companion(command)
        with self.assertRaises(ValueError):
            companion.run_once()
        self.assertEqual(window.colors, [])

    def test_capture_safety_failure_is_reported_as_unverified_not_success(self):
        command = make_command(self.session_id)
        companion, client, window, _ = self.companion(command, capture=FakeCapture(fail=True))
        self.assertEqual(companion.run_once(), "executed_unverified")
        self.assertEqual(window.colors, ["blue"])
        self.assertEqual(client.results[0]["p_status"], "executed_unverified")
        self.assertEqual(client.results[0]["p_error_code"], "capture_unsafe")
        self.assertIsNone(client.results[0]["p_screenshot_base64"])

    def test_window_exception_is_failed_and_does_not_expose_exception_text(self):
        command = make_command(self.session_id)
        companion, client, window, _ = self.companion(command, window=FakeWindow(fail=True))
        self.assertEqual(companion.run_once(), "failed")
        self.assertEqual(window.colors, [])
        self.assertEqual(client.results[0]["p_status"], "failed")
        self.assertEqual(client.results[0]["p_error_code"], "executor_failed")
        self.assertNotIn("internal error must not leak", repr(client.results))

    def test_failure_return_matches_accepted_result_and_reports_upload_failure(self):
        command = make_command(self.session_id)
        companion, client, _, _ = self.companion(command, window=FakeWindow(fail=True))
        self.assertEqual(companion.run_once(), "failed")
        self.assertEqual(client.results[0]["p_status"], "failed")

        command = make_command(self.session_id)
        client = FakeClient(self.session_id, command, publish_error=True)
        companion, _, window, _ = self.companion(command, window=FakeWindow(fail=True), client=client)
        self.assertEqual(companion.run_once(), "report_failed")
        self.assertEqual(companion.run_once(), "duplicate")
        self.assertEqual(window.colors, [])


if __name__ == "__main__":
    unittest.main()
