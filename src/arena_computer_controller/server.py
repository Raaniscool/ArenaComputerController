"""Small, dependency-free HTTP/JSON API for local controller clients."""

from __future__ import annotations

import hmac
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .controller import ComputerController
from .errors import ControllerError

_MAX_BODY_BYTES = 1_000_000


class ControllerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: ComputerController, token: str | None = None):
        self.controller = controller
        self.token = token
        super().__init__(address, ControllerRequestHandler)


def create_server(
    controller: ComputerController,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> ControllerHTTPServer:
    try:
        loopback = host.casefold() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not token:
        raise ValueError("Non-loopback controller binds require a bearer token.")
    return ControllerHTTPServer((host, port), controller, token)


class ControllerRequestHandler(BaseHTTPRequestHandler):
    server: ControllerHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "ArenaComputerController/0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            return
        try:
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query, keep_blank_values=True)

            if path == "/health":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "version": __version__,
                        "capabilities": self.server.controller.backend.capabilities(),
                    },
                )
                return
            if path == "/v1/state":
                self._send_json(200, self.server.controller.get_state())
                return
            if path == "/v1/inputs":
                self._send_json(200, self.server.controller.get_inputs())
                return
            if path == "/v1/history":
                limit = self._query_int(query, "limit", 50)
                self._send_json(200, {"history": self.server.controller.history(limit)})
                return
            if path == "/v1/screenshot":
                self._handle_screenshot(query)
                return
            self._send_json(404, self._error("not_found", "Unknown API endpoint."))
        except ControllerError as exc:
            self._send_json(exc.status, self._error(exc.code, exc.message))
        except Exception:
            self._send_json(500, self._error("internal_error", "Internal controller error."))

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            return
        try:
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            body = self._read_json_body()
            if path == "/v1/actions":
                self._send_json(200, self.server.controller.perform_action(body))
                return
            if path == "/v1/refresh":
                self._send_json(200, self.server.controller.refresh())
                return
            self._send_json(404, self._error("not_found", "Unknown API endpoint."))
        except ControllerError as exc:
            self._send_json(exc.status, self._error(exc.code, exc.message))
        except Exception:
            self._send_json(500, self._error("internal_error", "Internal controller error."))

    def _handle_screenshot(self, query: dict[str, list[str]]) -> None:
        image_format = self._query_one(query, "format", "png").lower()
        quality = self._query_int(query, "quality", 85)
        scale_text = self._query_one(query, "scale", "1")
        try:
            scale = float(scale_text)
        except ValueError:
            raise ControllerError("'scale' must be a number.", "invalid_screenshot_request", 400) from None

        window_id = self._query_one(query, "window_id", "") or None
        region_keys = ("x", "y", "width", "height")
        region_values = {key: self._query_one(query, key, "") for key in region_keys}
        has_region_part = any(value != "" for value in region_values.values())
        region: dict[str, int] | None = None
        if has_region_part:
            if any(value == "" for value in region_values.values()):
                raise ControllerError("A crop requires x, y, width, and height.", "invalid_region", 400)
            try:
                region = {key: int(value) for key, value in region_values.items()}
            except ValueError:
                raise ControllerError("Crop coordinates and dimensions must be integers.", "invalid_region", 400) from None

        data, metadata = self.server.controller.capture_screenshot(
            image_format=image_format,
            quality=quality,
            scale=scale,
            window_id=window_id,
            region=region,
        )
        self.send_response(200)
        self.send_header("Content-Type", metadata["mime_type"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Image-Width", str(metadata["width"]))
        self.send_header("X-Image-Height", str(metadata["height"]))
        self.send_header("X-Capture-Ms", str(metadata["capture_ms"]))
        self.send_header("X-Encode-Ms", str(metadata["encode_ms"]))
        self.send_header("X-Image-Bytes", str(metadata["bytes"]))
        self.send_header("X-Capture-Backend", str(metadata["capture_backend"]))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        # Health is intentionally public so a supervisor can detect a running service.
        if (urlsplit(self.path).path.rstrip("/") or "/") == "/health":
            return True
        expected = self.server.token
        if not expected:
            return True
        supplied = self.headers.get("Authorization", "")
        expected_header = f"Bearer {expected}"
        if hmac.compare_digest(supplied.encode("utf-8"), expected_header.encode("utf-8")):
            return True
        self._send_json(401, self._error("unauthorized", "A valid bearer token is required."))
        return False

    def _read_json_body(self) -> dict[str, Any]:
        content_length = self.headers.get("Content-Length", "0")
        try:
            length = int(content_length)
        except ValueError:
            raise ControllerError("Invalid Content-Length header.", "invalid_request", 400) from None
        if length < 0:
            raise ControllerError("Invalid Content-Length header.", "invalid_request", 400)
        if length > _MAX_BODY_BYTES:
            raise ControllerError("Request body exceeds 1 MB.", "payload_too_large", 413)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControllerError("Request body must be valid UTF-8 JSON.", "invalid_json", 400) from None
        if not isinstance(value, dict):
            raise ControllerError("Request body must be a JSON object.", "invalid_json", 400)
        return value

    @staticmethod
    def _query_one(query: dict[str, list[str]], key: str, default: str) -> str:
        values = query.get(key)
        if not values:
            return default
        return values[-1]

    def _query_int(self, query: dict[str, list[str]], key: str, default: int) -> int:
        value = self._query_one(query, key, str(default))
        try:
            return int(value)
        except ValueError:
            raise ControllerError(f"Query parameter '{key}' must be an integer.", "invalid_query", 400) from None

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}

    def log_message(self, _format: str, *_args: Any) -> None:
        # Avoid logging window titles, query strings, or accidental sensitive data.
        return
