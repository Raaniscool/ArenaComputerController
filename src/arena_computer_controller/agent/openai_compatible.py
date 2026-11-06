"""Small OpenAI Chat Completions-compatible provider using the Python stdlib.

This keeps the runtime free of SDK dependencies. Arena Router is the initial
recommended endpoint; compatible providers can be selected by configuration.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence

from .types import AssistantTurn, ChatMessage, ImageAttachment, ToolCall, ToolSpec

_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}


def _reject_nonstandard_json_constant(_value: str):
    raise ValueError("non-standard JSON constant")


class ModelProviderError(RuntimeError):
    """A safe, user-facing model request or response error."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward a model API bearer token to a redirect destination."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class OpenAICompatibleProvider:
    """Chat Completions adapter for APIs that support tool calls and images."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        opener: Any | None = None,
    ):
        self.base_url = self._validate_base_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("A model API key is required; set ARENA_MODEL_API_KEY.")
        if any(ord(char) < 32 or ord(char) == 127 for char in api_key):
            raise ValueError("The model API key contains unsupported control characters.")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("A model name is required.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("Model timeout must be a finite number of seconds.")
        try:
            timeout_value = float(timeout)
        except (OverflowError, ValueError):
            raise ValueError("Model timeout must be between 0.1 and 600 seconds.") from None
        if not math.isfinite(timeout_value) or not 0.1 <= timeout_value <= 600:
            raise ValueError("Model timeout must be between 0.1 and 600 seconds.")
        for label, value in (
            ("maximum request size", max_request_bytes),
            ("maximum response size", max_response_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label.capitalize()} must be a positive integer.")

        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout_value
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("Model API base URL must be a non-empty HTTP(S) URL.")
        normalized = base_url.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in base_url) or any(char.isspace() for char in normalized):
            raise ValueError("Model API base URL cannot contain whitespace or control characters.")
        try:
            parsed = urllib.parse.urlsplit(normalized)
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            raise ValueError("Model API base URL is malformed.") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Model API base URL must be HTTP(S), without credentials, query, or fragment.")
        if parsed.scheme == "http":
            try:
                loopback = host.casefold() == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise ValueError("Plain HTTP model URLs are allowed only for localhost test servers; use HTTPS remotely.")
        return normalized.rstrip("/")

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> AssistantTurn:
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": [self._serialize_message(message) for message in messages],
            }
            if tools:
                payload["tools"] = [self._serialize_tool(tool) for tool in tools]
                payload["tool_choice"] = "auto"
                payload["parallel_tool_calls"] = False
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ModelProviderError("The model request contains data that cannot be encoded as JSON.") from None
        if len(body) > self.max_request_bytes:
            raise ModelProviderError(
                f"The model request exceeds the configured {self.max_request_bytes}-byte size limit."
            )

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raise ModelProviderError(self._http_error_message(status), status=status) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError,)):
                raise ModelProviderError("The model API request timed out.") from None
            raise ModelProviderError("Could not reach the model API. Check the URL and network connection.") from None
        except TimeoutError:
            raise ModelProviderError("The model API request timed out.") from None
        except OSError:
            raise ModelProviderError("The model API connection failed.") from None

        if len(raw) > self.max_response_bytes:
            raise ModelProviderError("The model API response exceeded the configured size limit.", status=status)
        try:
            response_data = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ModelProviderError("The model API returned invalid JSON.", status=status) from None
        return self._parse_response(response_data, status=status)

    @staticmethod
    def _http_error_message(status: int) -> str:
        if status in {401, 403}:
            return f"The model API rejected authentication or workspace access (HTTP {status}). Check API-key and account access."
        if status == 404:
            return "The model API endpoint or model was not found (HTTP 404). Check the base URL and model name."
        if status == 429:
            return "The model API rate limit or quota was reached (HTTP 429). Check account usage and billing."
        if 400 <= status < 500:
            return f"The model API rejected the request (HTTP {status}). Check provider and model compatibility."
        return f"The model API returned an error (HTTP {status})."

    @staticmethod
    def _serialize_tool(tool: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            },
        }

    @classmethod
    def _serialize_message(cls, message: ChatMessage) -> dict[str, Any]:
        if message.role not in {"system", "user", "assistant", "tool"}:
            raise ModelProviderError("The conversation contains an unsupported message role.")
        content: Any = message.content
        if content is None and message.role != "assistant":
            content = ""
        if message.images:
            if message.role != "user":
                raise ModelProviderError("Visual attachments must be sent in a user message for this provider.")
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"type": "text", "text": message.content})
            for image in message.images:
                parts.append({"type": "image_url", "image_url": {"url": cls._data_url(image)}})
            content = parts

        result: dict[str, Any] = {"role": message.role, "content": content}
        if message.role == "tool":
            if not message.tool_call_id:
                raise ModelProviderError("A tool result is missing its tool-call ID.")
            result["tool_call_id"] = message.tool_call_id
            if message.name:
                result["name"] = message.name
        if message.role == "assistant" and message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        return result

    @staticmethod
    def _data_url(image: ImageAttachment) -> str:
        mime_type = image.mime_type.casefold() if isinstance(image.mime_type, str) else ""
        if mime_type not in _SUPPORTED_IMAGE_TYPES:
            raise ModelProviderError("The tool returned an unsupported image type.")
        if not isinstance(image.data, bytes) or not image.data:
            raise ModelProviderError("The tool returned an empty or invalid image payload.")
        encoded = base64.b64encode(image.data).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @classmethod
    def _parse_response(cls, value: Any, *, status: int) -> AssistantTurn:
        if not isinstance(value, dict):
            raise ModelProviderError("The model API returned an invalid response object.", status=status)
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ModelProviderError("The model API response did not contain a completion choice.", status=status)
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ModelProviderError("The model API response did not contain an assistant message.", status=status)

        content_value = message.get("content")
        if content_value is None:
            content: str | None = None
        elif isinstance(content_value, str):
            content = content_value
        elif isinstance(content_value, list):
            text_parts = [
                item.get("text")
                for item in content_value
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"} and isinstance(item.get("text"), str)
            ]
            content = "\n".join(text_parts)
        else:
            raise ModelProviderError("The model API returned an unsupported assistant content type.", status=status)

        raw_calls = message.get("tool_calls", [])
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise ModelProviderError("The model API returned malformed tool calls.", status=status)
        calls: list[ToolCall] = []
        seen_call_ids: set[str] = set()
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise ModelProviderError("The model API returned a malformed tool call.", status=status)
            call_id = raw_call.get("id")
            function = raw_call.get("function")
            if not isinstance(call_id, str) or not call_id or len(call_id) > 256 or not isinstance(function, dict):
                raise ModelProviderError("The model API returned an incomplete tool call.", status=status)
            if call_id in seen_call_ids:
                raise ModelProviderError("The model API returned duplicate tool-call IDs.", status=status)
            seen_call_ids.add(call_id)
            name = function.get("name")
            raw_arguments = function.get("arguments")
            if not isinstance(name, str) or not name or len(name) > 128:
                raise ModelProviderError("The model API returned a tool call without a valid name.", status=status)
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(
                        raw_arguments,
                        parse_constant=_reject_nonstandard_json_constant,
                    )
                except (ValueError, RecursionError):
                    raise ModelProviderError("The model API returned invalid JSON tool arguments.", status=status) from None
            else:
                arguments = raw_arguments
            if not isinstance(arguments, dict):
                raise ModelProviderError("Tool arguments must be a JSON object.", status=status)
            calls.append(ToolCall(id=call_id, name=name, arguments=arguments))

        finish_reason = choices[0].get("finish_reason")
        return AssistantTurn(
            content=content,
            tool_calls=tuple(calls),
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        )
