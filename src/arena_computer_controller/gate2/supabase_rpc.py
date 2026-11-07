"""Small, fixed-endpoint Supabase Data API client for the Gate 2 RPCs.

No table CRUD, arbitrary URL, service/secret key, redirects, or token-bearing
query parameters are supported.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .credential_store import LocalConfig, validate_public_api_key, validate_project_url
from .protocol import validate_pairing_token


class SupabaseRpcError(RuntimeError):
    """Sanitized transport/authentication failure; never contains request secrets."""

    def __init__(self, message: str, *, http_status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.http_status = http_status
        self.retryable = retryable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        # Do not risk forwarding the pairing token to a redirect target.
        return None


class SupabaseRpcClient:
    _RPC_NAMES = frozenset(
        {
            "gate2_ping",
            "gate2_claim_command",
            "gate2_publish_initial_snapshot",
            "gate2_publish_result",
        }
    )
    _MAX_RESPONSE_BYTES = 256_000

    def __init__(
        self,
        config: LocalConfig,
        pairing_token: str,
        *,
        timeout_seconds: float = 10.0,
        opener: Any | None = None,
    ) -> None:
        self._project_url = validate_project_url(config.project_url)
        self._api_key = validate_public_api_key(config.publishable_key)
        validate_pairing_token(pairing_token)
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("RPC timeout must be between 1 and 30 seconds.")
        self._pairing_token = pairing_token
        self._timeout = timeout_seconds
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def _post(self, rpc_name: str, body: dict[str, Any]) -> Any:
        if rpc_name not in self._RPC_NAMES:
            raise ValueError("Unsupported Gate 2 RPC.")
        url = f"{self._project_url}/rest/v1/rpc/{rpc_name}"
        headers = {
            "apikey": self._api_key,
            "x-arena-gate2-token": self._pairing_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cache-Control": "no-store",
        }
        # New sb_publishable keys are not JWTs and must not be sent as Bearer tokens.
        # The legacy anon key is a JWT and is used as the Authorization identity.
        if self._api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            response = self._opener.open(request, timeout=self._timeout)
            try:
                raw = response.read(self._MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", 200)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                exc.close()
            except Exception:
                pass
            retryable = status in {408, 425, 429} or status >= 500
            message = "Pairing rejected by Supabase." if status in {401, 403} else f"Supabase RPC failed (HTTP {status})."
            raise SupabaseRpcError(message, http_status=status, retryable=retryable) from None
        except urllib.error.URLError:
            raise SupabaseRpcError("Supabase is unreachable; check the network and project URL.", retryable=True) from None
        except TimeoutError:
            raise SupabaseRpcError("Supabase request timed out.", retryable=True) from None
        except OSError:
            raise SupabaseRpcError("Supabase transport failed.", retryable=True) from None

        if status < 200 or status >= 300:
            retryable = status in {408, 425, 429} or status >= 500
            raise SupabaseRpcError(f"Supabase RPC failed (HTTP {status}).", http_status=status, retryable=retryable)
        if len(raw) > self._MAX_RESPONSE_BYTES:
            raise SupabaseRpcError("Supabase RPC response exceeded the size limit.")
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeError, json.JSONDecodeError):
            raise SupabaseRpcError("Supabase returned an invalid RPC response.") from None

    def ping(self) -> Any:
        return self._post("gate2_ping", {})

    def claim_next_command(self) -> Any:
        return self._post("gate2_claim_command", {})

    def publish_initial_snapshot(self, payload: dict[str, Any]) -> Any:
        return self._post("gate2_publish_initial_snapshot", payload)

    def publish_result(self, payload: dict[str, Any]) -> Any:
        return self._post("gate2_publish_result", payload)
