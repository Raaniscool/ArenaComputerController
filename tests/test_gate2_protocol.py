from __future__ import annotations

import struct
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from arena_computer_controller.gate2.protocol import (
    ALLOWED_COLORS,
    ProtocolError,
    build_initial_snapshot_payload,
    build_result_payload,
    parse_claim_response,
    screenshot_fields,
)


def make_png(width: int = 32, height: int = 24, rgb: tuple[int, int, int] = (50, 100, 200)) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class Gate2ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        self.session_id = str(uuid4())
        self.command_id = str(uuid4())
        self.request_id = str(uuid4())

    def claim_response(self, **command_changes):
        command = {
            "command_id": self.command_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "operation": "set_marker_color",
            "color": "blue",
            "issued_at": (self.now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (self.now + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            "status": "claimed",
        }
        command.update(command_changes)
        return {
            "ok": True,
            "session_id": self.session_id,
            "session_expires_at": (self.now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "command": command,
        }

    def test_valid_command_is_parsed_strictly(self) -> None:
        command = parse_claim_response(self.claim_response(), self.session_id, now=self.now)
        self.assertEqual(command.command_id, self.command_id)
        self.assertEqual(command.operation, "set_marker_color")
        self.assertIn(command.color, ALLOWED_COLORS)

    def test_no_pending_command_is_idle(self) -> None:
        response = self.claim_response()
        response["command"] = None
        self.assertIsNone(parse_claim_response(response, self.session_id, now=self.now))

    def test_rejects_other_session_invalid_action_and_color(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_claim_response(self.claim_response(session_id=str(uuid4())), self.session_id, now=self.now)
        with self.assertRaises(ProtocolError):
            parse_claim_response(self.claim_response(operation="press_key"), self.session_id, now=self.now)
        with self.assertRaises(ProtocolError):
            parse_claim_response(self.claim_response(color="magenta"), self.session_id, now=self.now)

    def test_rejects_expired_long_lived_and_unclaimed_commands(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_claim_response(
                self.claim_response(expires_at=(self.now - timedelta(seconds=1)).isoformat()),
                self.session_id,
                now=self.now,
            )
        with self.assertRaises(ProtocolError):
            parse_claim_response(
                self.claim_response(expires_at=(self.now + timedelta(seconds=45)).isoformat()),
                self.session_id,
                now=self.now,
            )
        with self.assertRaises(ProtocolError):
            parse_claim_response(self.claim_response(status="pending"), self.session_id, now=self.now)

    def test_rejects_extra_command_fields_and_expired_session(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_claim_response(self.claim_response(marker_color="red"), self.session_id, now=self.now)
        response = self.claim_response()
        response["session_expires_at"] = (self.now - timedelta(seconds=1)).isoformat()
        with self.assertRaises(ProtocolError):
            parse_claim_response(response, self.session_id, now=self.now)

    def test_png_payload_is_validated_and_bounds_checked(self) -> None:
        png = make_png()
        fields = screenshot_fields(png, 32, 24)
        self.assertEqual(fields["width"], 32)
        with self.assertRaises(ProtocolError):
            screenshot_fields(png[:-1], 32, 24)
        with self.assertRaises(ProtocolError):
            screenshot_fields(png, 31, 24)
        with self.assertRaises(ProtocolError):
            screenshot_fields(png, 961, 24)

    def test_result_and_snapshot_payloads_never_include_marker_color(self) -> None:
        png = make_png()
        result = build_result_payload(self.command_id, "succeeded", png=png, width=32, height=24)
        snapshot = build_initial_snapshot_payload(str(uuid4()), png, 32, 24)
        for payload in (result, snapshot):
            self.assertNotIn("color", payload)
            self.assertNotIn("marker_color", payload)
            self.assertNotIn("observed_color", payload)
        self.assertEqual(result["p_status"], "succeeded")

    def test_failure_result_requires_fixed_error_code_and_no_color_field(self) -> None:
        result = build_result_payload(self.command_id, "executed_unverified", error_code="capture_unsafe")
        self.assertEqual(result["p_error_code"], "capture_unsafe")
        self.assertNotIn("color", result)
        with self.assertRaises(ProtocolError):
            build_result_payload(self.command_id, "failed", error_code="C:\\Users\\Private\\file.txt")


if __name__ == "__main__":
    unittest.main()
