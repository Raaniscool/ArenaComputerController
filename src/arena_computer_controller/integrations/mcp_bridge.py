"""Local, dependency-free tool bridge for the ArenaComputerController HTTP API.

The MCP transport layer lives in ``arena_computer_controller.mcp_server``. This
module keeps HTTP handling, validation, safe errors, image limits, and duplicate
image suppression testable without installing an MCP SDK.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

_MAX_JSON_BYTES = 1_000_000
_DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 540.0
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"


class BridgeError(ValueError):
    """A safe, structured validation or transport failure."""

    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ControllerHTTPError(BridgeError):
    """An HTTP error returned by the local controller."""

    def __init__(self, status: int, payload: Any):
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        super().__init__(
            code if isinstance(code, str) else "controller_http_error",
            message if isinstance(message, str) else f"Controller returned HTTP {status}.",
            status=status,
        )
        self.payload = payload


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class ToolOutcome:
    """MCP-ready structured metadata plus an optional native image payload."""

    data: dict[str, Any]
    image: bytes | None = None
    image_mime_type: str | None = None
    is_error: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        # Never forward a controller bearer token to a redirect target.
        return None


class ControllerHTTPClient:
    """Small HTTP client that deliberately permits loopback destinations only."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        token: str | None = None,
        *,
        max_image_bytes: int = _DEFAULT_MAX_IMAGE_BYTES,
        opener: Any | None = None,
    ):
        self.base_url = self._validate_base_url(base_url)
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise BridgeError("invalid_controller_token", "Controller token must be a non-empty string when set.")
        if isinstance(max_image_bytes, bool) or not isinstance(max_image_bytes, int):
            raise BridgeError("invalid_image_limit", "Image byte limit must be an integer.")
        if not 1024 <= max_image_bytes <= _MAX_IMAGE_BYTES:
            raise BridgeError("invalid_image_limit", "Image byte limit must be between 1 KiB and 20 MiB.")
        self.token = token
        self.max_image_bytes = max_image_bytes
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise BridgeError("invalid_controller_url", "Controller URL must be a non-empty loopback URL.")
        try:
            parsed = urllib.parse.urlsplit(base_url.strip())
            hostname = parsed.hostname
            _ = parsed.port  # Validate the optional port before retaining the origin.
        except ValueError:
            raise BridgeError("invalid_controller_url", "Controller URL is malformed.") from None
        if parsed.scheme != "http" or not hostname or parsed.username or parsed.password:
            raise BridgeError("invalid_controller_url", "Controller URL must use HTTP and contain no credentials.")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise BridgeError("invalid_controller_url", "Controller URL must be an origin without a path or query.")
        if hostname.casefold() != "localhost":
            try:
                if not ipaddress.ip_address(hostname).is_loopback:
                    raise BridgeError("non_loopback_controller", "Controller URL must resolve to a loopback address.")
            except ValueError:
                raise BridgeError("non_loopback_controller", "Controller URL must use localhost or a loopback IP.") from None
        return f"{parsed.scheme}://{parsed.netloc}"

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 20.0,
        max_bytes: int = _MAX_JSON_BYTES,
    ) -> HTTPResponse:
        if not path.startswith("/") or path.startswith("//"):
            raise BridgeError("invalid_controller_path", "Controller API path must be an absolute local path.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise BridgeError("invalid_timeout", "Request timeout must be a finite number of seconds.")
        try:
            timeout_value = float(timeout)
        except (OverflowError, ValueError):
            raise BridgeError("invalid_timeout", "Request timeout is outside the supported range.") from None
        if not math.isfinite(timeout_value) or not 0.1 <= timeout_value <= _MAX_TIMEOUT_SECONDS + 10:
            raise BridgeError("invalid_timeout", "Request timeout is outside the supported range.")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise BridgeError("invalid_response_limit", "Response size limit must be a positive integer.")

        body = None
        headers = {"Accept": "application/json, image/png, image/jpeg, image/webp"}
        if payload is not None:
            try:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise BridgeError("invalid_json_payload", "Controller request must contain finite JSON values.") from None
            if len(body) > _MAX_JSON_BYTES:
                raise BridgeError("request_too_large", "Controller request exceeds the configured 1 MB JSON limit.")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout_value) as response:
                data = response.read(max_bytes + 1)
                status = int(response.status)
                response_headers = {key.casefold(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            error_body = exc.read(min(max_bytes, _MAX_JSON_BYTES) + 1)
            if len(error_body) > min(max_bytes, _MAX_JSON_BYTES):
                error_body = b""
            payload_value = self._decode_json(error_body)
            raise ControllerHTTPError(exc.code, payload_value) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            code = "controller_unreachable"
            message = "The local controller could not be reached at its loopback URL."
            if isinstance(reason, TimeoutError):
                code, message = "controller_timeout", "The local controller request timed out."
            raise BridgeError(code, message) from None
        except TimeoutError:
            raise BridgeError("controller_timeout", "The local controller request timed out.") from None
        except OSError:
            raise BridgeError("controller_connection_error", "The local controller connection failed.") from None

        if len(data) > max_bytes:
            raise BridgeError("response_too_large", "Controller response exceeds the configured size limit.", status=status)
        if status >= 400:
            raise ControllerHTTPError(status, self._decode_json(data))
        return HTTPResponse(status, response_headers, data)

    @staticmethod
    def _decode_json(data: bytes) -> Any:
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None

    def get_json(self, path: str, *, timeout: float = 20.0) -> dict[str, Any]:
        response = self.request("GET", path, timeout=timeout, max_bytes=_MAX_JSON_BYTES)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise BridgeError("invalid_controller_json", "Controller returned invalid JSON.", status=response.status) from None
        if not isinstance(value, dict):
            raise BridgeError("invalid_controller_json", "Controller returned a non-object JSON response.", status=response.status)
        return value

    def post_json(self, path: str, payload: dict[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
        response = self.request("POST", path, payload=payload, timeout=timeout, max_bytes=_MAX_JSON_BYTES)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise BridgeError("invalid_controller_json", "Controller returned invalid JSON.", status=response.status) from None
        if not isinstance(value, dict):
            raise BridgeError("invalid_controller_json", "Controller returned a non-object JSON response.", status=response.status)
        return value

    def get_image(self, path: str, *, timeout: float = 30.0) -> HTTPResponse:
        response = self.request("GET", path, timeout=timeout, max_bytes=self.max_image_bytes)
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise BridgeError("invalid_image_content_type", "Controller did not return a supported image type.", status=response.status)
        self._validate_image(response.body, mime_type)
        return response

    @staticmethod
    def _validate_image(data: bytes, mime_type: str) -> None:
        valid = (
            mime_type == "image/png" and data.startswith(_PNG_SIGNATURE)
            or mime_type == "image/jpeg" and data.startswith(_JPEG_SIGNATURE)
            or mime_type == "image/webp" and len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        )
        if not valid:
            raise BridgeError("invalid_image_payload", "Controller image bytes do not match their declared format.")


class ControllerMCPBridge:
    """Validate tool inputs and produce structured responses/image payloads."""

    def __init__(self, client: ControllerHTTPClient | None = None):
        self.client = client or ControllerHTTPClient()
        self._last_image_hash: dict[str, str] = {}

    @staticmethod
    def _error(error: BridgeError, **extra: Any) -> ToolOutcome:
        data: dict[str, Any] = {
            "ok": False,
            "error": {"code": error.code, "message": error.message},
        }
        if error.status is not None:
            data["http_status"] = error.status
        data.update(extra)
        if isinstance(error, ControllerHTTPError) and isinstance(error.payload, dict):
            data["controller_response"] = error.payload
        return ToolOutcome(data, is_error=True)

    @staticmethod
    def _validate_timeout(timeout: Any) -> float:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise BridgeError("invalid_timeout", "timeout must be a finite number of seconds.")
        try:
            value = float(timeout)
        except (OverflowError, ValueError):
            raise BridgeError("invalid_timeout", "timeout is outside the supported range.") from None
        if not math.isfinite(value) or not 0.1 <= value <= _MAX_TIMEOUT_SECONDS:
            raise BridgeError("invalid_timeout", f"timeout must be between 0.1 and {_MAX_TIMEOUT_SECONDS:g} seconds.")
        return value

    def get_computer_state(self) -> ToolOutcome:
        try:
            return ToolOutcome(self.client.get_json("/v1/state"))
        except BridgeError as exc:
            return self._error(exc)

    def get_active_window(self) -> ToolOutcome:
        try:
            state = self.client.get_json("/v1/state")
            active = state.get("active_window")
            return ToolOutcome({
                "ok": True,
                "active_window": active,
                "timestamp": state.get("timestamp"),
            })
        except BridgeError as exc:
            return self._error(exc)

    @staticmethod
    def _validate_desktop_screenshot(format: str, quality: int, scale: float) -> tuple[str, int, float]:
        if not isinstance(format, str) or format.casefold() not in {"png", "jpeg", "jpg", "webp"}:
            raise BridgeError("invalid_screenshot_format", "format must be png, jpeg, or webp.")
        normalized = "jpeg" if format.casefold() in {"jpg", "jpeg"} else format.casefold()
        if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
            raise BridgeError("invalid_screenshot_quality", "quality must be an integer from 1 to 100.")
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            raise BridgeError("invalid_screenshot_scale", "scale must be between 0.1 and 1.0.")
        try:
            scale_value = float(scale)
        except (OverflowError, ValueError):
            raise BridgeError("invalid_screenshot_scale", "scale must be between 0.1 and 1.0.") from None
        if not math.isfinite(scale_value) or not 0.1 <= scale_value <= 1.0:
            raise BridgeError("invalid_screenshot_scale", "scale must be between 0.1 and 1.0.")
        return normalized, quality, scale_value

    def get_screenshot(
        self,
        format: str = "jpeg",
        quality: int = 90,
        scale: float = 0.75,
        force: bool = False,
        window_id: str | None = None,
    ) -> ToolOutcome:
        try:
            if not isinstance(force, bool):
                raise BridgeError("invalid_force", "force must be a boolean.")
            if window_id is not None and (
                not isinstance(window_id, str)
                or not window_id
                or len(window_id) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in window_id)
            ):
                raise BridgeError("invalid_window_id", "window_id must be a non-empty, printable window ID up to 256 characters.")
            normalized, quality, scale = self._validate_desktop_screenshot(format, quality, scale)
            query_values: dict[str, Any] = {"format": normalized, "quality": quality, "scale": scale}
            if window_id is not None:
                query_values["window_id"] = window_id
            query = urllib.parse.urlencode(query_values)
            response = self.client.get_image("/v1/screenshot?" + query)
            return self._image_outcome("desktop", response.body, response.headers, force=force, window_id=window_id)
        except BridgeError as exc:
            return self._error(exc)

    def blender_probe(self, timeout: float = 5.0) -> ToolOutcome:
        try:
            value = self._validate_timeout(timeout)
            response = self.client.post_json("/v1/integrations/blender/probe", {"timeout": value}, timeout=value + 10)
            return ToolOutcome(response, is_error=not bool(response.get("healthy")))
        except BridgeError as exc:
            return self._error(exc)

    def blender_execute(
        self,
        operation: str,
        parameters: dict[str, Any] | None = None,
        timeout: float = 60.0,
        request_id: str | None = None,
    ) -> ToolOutcome:
        if not isinstance(operation, str) or not operation.strip():
            return self._error(BridgeError("invalid_operation", "operation must be a non-empty string."))
        if parameters is not None and not isinstance(parameters, dict):
            return self._error(BridgeError("invalid_parameters", "parameters must be a JSON object."))
        try:
            value = self._validate_timeout(timeout)
            if request_id is None:
                request_id = str(uuid.uuid4())
            if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 512:
                raise BridgeError("invalid_request_id", "request_id must be a non-empty string up to 512 characters.")
            response = self.client.post_json(
                "/v1/integrations/blender/commands",
                {
                    "operation": operation,
                    "parameters": parameters,
                    "timeout": value,
                    "request_id": request_id,
                },
                timeout=value + 10,
            )
            response = dict(response)
            response["request_id"] = request_id
            return ToolOutcome(response, is_error=not bool(response.get("ok")))
        except BridgeError as exc:
            return self._error(exc, request_id=request_id)

    def blender_get_screenshot(self, job_id: str, force: bool = False) -> ToolOutcome:
        if not isinstance(force, bool):
            return self._error(BridgeError("invalid_force", "force must be a boolean."))
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            return self._error(BridgeError("invalid_job_id", "job_id has an invalid format."))
        try:
            path = "/v1/integrations/blender/jobs/{}/screenshot".format(urllib.parse.quote(job_id, safe=""))
            response = self.client.get_image(path)
            return self._image_outcome("blender", response.body, response.headers, force=force, job_id=job_id)
        except BridgeError as exc:
            return self._error(exc, job_id=job_id)

    def _image_outcome(
        self,
        channel: str,
        image: bytes,
        headers: Mapping[str, str],
        *,
        force: bool,
        job_id: str | None = None,
        window_id: str | None = None,
    ) -> ToolOutcome:
        if not isinstance(force, bool):
            raise BridgeError("invalid_force", "force must be a boolean.")
        digest = hashlib.sha256(image).hexdigest()
        same_as_previous = self._last_image_hash.get(channel) == digest
        metadata: dict[str, Any] = {
            "ok": True,
            "image_included": bool(force or not same_as_previous),
            "identical_to_previous": same_as_previous,
            "mime_type": headers.get("content-type", "").split(";", 1)[0].strip().casefold(),
            "bytes": len(image),
            "sha256": digest,
        }
        for source, target in (("x-image-width", "width"), ("x-image-height", "height"), ("x-image-bytes", "reported_bytes")):
            raw = headers.get(source)
            if isinstance(raw, str) and raw.isdigit():
                metadata[target] = int(raw)
        if job_id is not None:
            metadata["job_id"] = job_id
            metadata["capture_type"] = headers.get("x-blender-capture-type", "camera_render")
        if window_id is not None:
            metadata["window_id"] = window_id
        self._last_image_hash[channel] = digest
        if same_as_previous and not force:
            metadata["note"] = "This image is byte-identical to the previous image from this tool; no image payload was resent. Set force=true to include it again."
            return ToolOutcome(metadata)
        return ToolOutcome(metadata, image=image, image_mime_type=metadata["mime_type"])
