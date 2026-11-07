"""Strict wire-format validation for the Gate 2 companion.

The initial marker color is intentionally absent from every outbound payload.
Only pixels in the screenshot reveal that state.
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID


MARKER_COLORS: dict[str, str] = {
    "red": "#D94848",
    "green": "#249B72",
    "blue": "#3678D4",
    "yellow": "#E5B52E",
    "purple": "#8756BD",
}
ALLOWED_COLORS = frozenset(MARKER_COLORS)
COMMAND_TTL_SECONDS = 30
MAX_SCREENSHOT_BYTES = 180_000
MAX_SCREENSHOT_BASE64_CHARS = 4 * ((MAX_SCREENSHOT_BYTES + 2) // 3)
MAX_IMAGE_WIDTH = 960
MAX_IMAGE_HEIGHT = 720
MAX_IMAGE_PIXELS = 960 * 720
COMMAND_OPERATIONS = frozenset({"set_marker_color"})
RESULT_STATUSES = frozenset({"succeeded", "failed", "executed_unverified"})
ERROR_CODES = frozenset({"executor_failed", "capture_unsafe", "capture_failed", "screenshot_too_large"})
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ProtocolError(ValueError):
    """A malformed, stale, or unsupported Gate 2 message."""


@dataclass(frozen=True)
class ClaimedCommand:
    command_id: str
    request_id: str
    session_id: str
    operation: str
    color: str
    issued_at: datetime
    expires_at: datetime


def validate_pairing_token(token: str) -> None:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ProtocolError("The pairing token is missing or malformed.")


def normalize_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field_name} must be a UUID string.")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise ProtocolError(f"{field_name} must be a UUID string.") from None


def parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{field_name} must be an ISO timestamp.")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ProtocolError(f"{field_name} must be an ISO timestamp.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def parse_claim_response(
    payload: Any,
    expected_session_id: str,
    *,
    now: datetime | None = None,
) -> ClaimedCommand | None:
    """Validate the exact result of ``gate2_claim_command`` before execution."""
    expected_session_id = normalize_uuid(expected_session_id, "expected session_id")
    if not isinstance(payload, dict):
        raise ProtocolError("Claim response must be an object.")
    if set(payload) != {"ok", "session_id", "session_expires_at", "command"}:
        raise ProtocolError("Claim response has an unexpected shape.")
    if payload.get("ok") is not True:
        raise ProtocolError("Claim was not accepted.")
    response_session = normalize_uuid(payload.get("session_id"), "session_id")
    if response_session != expected_session_id:
        raise ProtocolError("Claim response belongs to another test session.")

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    session_expiry = parse_timestamp(payload.get("session_expires_at"), "session_expires_at")
    if session_expiry <= current:
        raise ProtocolError("The paired test session has expired.")

    raw_command = payload.get("command")
    if raw_command is None:
        return None
    if not isinstance(raw_command, dict):
        raise ProtocolError("Claimed command must be an object or null.")
    expected_fields = {
        "command_id",
        "request_id",
        "session_id",
        "operation",
        "color",
        "issued_at",
        "expires_at",
        "status",
    }
    if set(raw_command) != expected_fields:
        raise ProtocolError("Command has an unexpected shape.")

    command_session = normalize_uuid(raw_command.get("session_id"), "command session_id")
    if command_session != expected_session_id:
        raise ProtocolError("Command belongs to another test session.")
    command_id = normalize_uuid(raw_command.get("command_id"), "command_id")
    request_id = normalize_uuid(raw_command.get("request_id"), "request_id")
    operation = raw_command.get("operation")
    if not isinstance(operation, str) or operation not in COMMAND_OPERATIONS:
        raise ProtocolError("Command operation is not allowlisted.")
    color = raw_command.get("color")
    if not isinstance(color, str) or color not in ALLOWED_COLORS:
        raise ProtocolError("Command color is not allowlisted.")
    if raw_command.get("status") != "claimed":
        raise ProtocolError("Command was not atomically claimed.")

    issued_at = parse_timestamp(raw_command.get("issued_at"), "issued_at")
    expires_at = parse_timestamp(raw_command.get("expires_at"), "expires_at")
    if issued_at > current + timedelta(seconds=5):
        raise ProtocolError("Command issue time is unexpectedly in the future.")
    ttl = expires_at - issued_at
    if ttl <= timedelta(0) or ttl > timedelta(seconds=COMMAND_TTL_SECONDS):
        raise ProtocolError("Command lifetime exceeds the allowed limit.")
    if expires_at <= current:
        raise ProtocolError("Command has expired.")
    if expires_at - current > timedelta(seconds=COMMAND_TTL_SECONDS):
        raise ProtocolError("Command expiry is too far in the future.")

    return ClaimedCommand(
        command_id=command_id,
        request_id=request_id,
        session_id=command_session,
        operation=operation,
        color=color,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _validate_png(png: bytes, width: int, height: int) -> None:
    if not isinstance(png, bytes) or len(png) < 45 or len(png) > MAX_SCREENSHOT_BYTES:
        raise ProtocolError("Screenshot is empty or exceeds the size limit.")
    if png[:8] != _PNG_SIGNATURE:
        raise ProtocolError("Screenshot is not a PNG image.")
    if (
        width < 1
        or height < 1
        or width > MAX_IMAGE_WIDTH
        or height > MAX_IMAGE_HEIGHT
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise ProtocolError("Screenshot dimensions are invalid or inconsistent.")

    offset = 8
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    image_data = bytearray()
    bit_depth = color_type = interlace_method = -1
    while offset + 12 <= len(png):
        chunk_length = struct.unpack_from(">I", png, offset)[0]
        chunk_type_start = offset + 4
        chunk_data_start = offset + 8
        chunk_end = chunk_data_start + chunk_length
        crc_end = chunk_end + 4
        if chunk_length > MAX_SCREENSHOT_BYTES or crc_end > len(png):
            raise ProtocolError("PNG chunk length is invalid.")
        chunk_type = png[chunk_type_start:chunk_data_start]
        chunk_data = png[chunk_data_start:chunk_end]
        expected_crc = struct.unpack_from(">I", png, chunk_end)[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ProtocolError("PNG chunk checksum is invalid.")
        if not saw_ihdr:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise ProtocolError("PNG must begin with one valid IHDR chunk.")
            actual_width, actual_height, bit_depth, color_type, compression, filtering, interlace_method = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if actual_width != width or actual_height != height:
                raise ProtocolError("Screenshot dimensions are inconsistent.")
            if compression != 0 or filtering != 0 or interlace_method != 0:
                raise ProtocolError("PNG encoding mode is unsupported.")
            if bit_depth != 8 or color_type not in {2, 6}:
                raise ProtocolError("PNG pixel format is unsupported.")
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise ProtocolError("PNG contains a duplicate IHDR chunk.")
        elif chunk_type == b"IDAT":
            if saw_iend:
                raise ProtocolError("PNG contains image data after IEND.")
            saw_idat = True
            image_data.extend(chunk_data)
        elif chunk_type == b"IEND":
            if chunk_length != 0 or not saw_idat:
                raise ProtocolError("PNG end chunk is invalid.")
            saw_iend = True
            offset = crc_end
            break
        elif chunk_type[:1].isupper():
            # Reject unknown critical chunks; ancillary chunks are harmless.
            raise ProtocolError("PNG contains an unsupported critical chunk.")
        offset = crc_end

    if not saw_ihdr or not saw_idat or not saw_iend or offset != len(png):
        raise ProtocolError("PNG is incomplete or has trailing data.")

    bytes_per_pixel = 4 if color_type == 6 else 3
    expected_raw_size = (width * bytes_per_pixel + 1) * height
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(bytes(image_data), expected_raw_size + 1)
    except zlib.error:
        raise ProtocolError("PNG pixel data is corrupt.") from None
    if (
        len(raw) != expected_raw_size
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise ProtocolError("PNG pixel data is incomplete or oversized.")
    row_stride = width * bytes_per_pixel + 1
    if any(raw[row * row_stride] > 4 for row in range(height)):
        raise ProtocolError("PNG contains an invalid row filter.")


def screenshot_fields(png: bytes, width: int, height: int) -> dict[str, Any]:
    """Return bounded image fields; no marker-state text is emitted."""
    if isinstance(width, bool) or not isinstance(width, int):
        raise ProtocolError("Screenshot width must be an integer.")
    if isinstance(height, bool) or not isinstance(height, int):
        raise ProtocolError("Screenshot height must be an integer.")
    _validate_png(png, width, height)
    encoded = base64.b64encode(png).decode("ascii")
    if len(encoded) > MAX_SCREENSHOT_BASE64_CHARS:
        raise ProtocolError("Screenshot exceeds the encoded size limit.")
    return {
        "screenshot_base64": encoded,
        "width": width,
        "height": height,
    }


def build_initial_snapshot_payload(
    snapshot_id: str, png: bytes, width: int, height: int
) -> dict[str, Any]:
    snapshot_id = normalize_uuid(snapshot_id, "snapshot_id")
    image = screenshot_fields(png, width, height)
    return {
        "p_snapshot_id": snapshot_id,
        "p_screenshot_base64": image["screenshot_base64"],
        "p_width": image["width"],
        "p_height": image["height"],
    }


def build_result_payload(
    command_id: str,
    status: str,
    *,
    png: bytes | None = None,
    width: int | None = None,
    height: int | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a minimal result; the commanded/observed color is never included."""
    command_id = normalize_uuid(command_id, "command_id")
    if not isinstance(status, str) or status not in RESULT_STATUSES:
        raise ProtocolError("Result status is not allowed.")
    if status == "succeeded":
        if png is None or width is None or height is None or error_code is not None:
            raise ProtocolError("A successful result requires a screenshot and no error code.")
        image = screenshot_fields(png, width, height)
    else:
        if not isinstance(error_code, str) or error_code not in ERROR_CODES:
            raise ProtocolError("A non-success result requires an allowlisted error code.")
        if png is None:
            if width is not None or height is not None:
                raise ProtocolError("Image dimensions cannot be supplied without an image.")
            image = {"screenshot_base64": None, "width": None, "height": None}
        else:
            if width is None or height is None:
                raise ProtocolError("Screenshot dimensions are required.")
            image = screenshot_fields(png, width, height)
    return {
        "p_command_id": command_id,
        "p_status": status,
        "p_screenshot_base64": image["screenshot_base64"],
        "p_width": image["width"],
        "p_height": image["height"],
        "p_error_code": error_code,
    }


def result_contains_marker_color(payload: Mapping[str, Any]) -> bool:
    """Test helper: reject accidental color-valued result metadata."""
    forbidden_fields = {"color", "marker_color", "observed_color", "current_color"}
    return any(str(key).casefold() in forbidden_fields for key in payload)
