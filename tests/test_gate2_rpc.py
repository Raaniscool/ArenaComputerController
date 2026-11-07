from __future__ import annotations

import base64
import json
import unittest
import urllib.error

from arena_computer_controller.gate2.credential_store import LocalConfig
from arena_computer_controller.gate2.supabase_rpc import SupabaseRpcClient, SupabaseRpcError


def legacy_anon_key() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"role": "anon"}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + payload + ".signature"


class FakeResponse:
    status = 200

    def __init__(self, body=b'{"ok":true}'):
        self.body = body

    def read(self, limit):
        return self.body[:limit]

    def close(self):
        pass


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        if self.error:
            raise self.error
        return self.response


class Gate2RpcTests(unittest.TestCase):
    def test_publishable_key_uses_apikey_and_never_bearer(self):
        key = "sb_publishable_" + "p" * 36
        token = "T" * 43
        opener = FakeOpener()
        client = SupabaseRpcClient(
            LocalConfig("https://example-ref.supabase.co", key),
            token,
            opener=opener,
        )
        self.assertEqual(client.ping(), {"ok": True})
        headers = {name.lower(): value for name, value in opener.request.header_items()}
        self.assertEqual(headers["apikey"], key)
        self.assertNotIn("authorization", headers)
        self.assertEqual(headers["x-arena-gate2-token"], token)
        self.assertNotIn(token, opener.request.full_url)
        self.assertNotIn(token, opener.request.data.decode())
        self.assertEqual(opener.request.full_url, "https://example-ref.supabase.co/rest/v1/rpc/gate2_ping")

    def test_legacy_anon_jwt_is_sent_as_identity_but_service_role_is_rejected(self):
        key = legacy_anon_key()
        opener = FakeOpener()
        client = SupabaseRpcClient(LocalConfig("https://example-ref.supabase.co", key), "T" * 43, opener=opener)
        client.ping()
        headers = {name.lower(): value for name, value in opener.request.header_items()}
        self.assertEqual(headers["authorization"], f"Bearer {key}")

    def test_rpc_endpoint_names_are_fixed_and_unknown_names_rejected(self):
        client = SupabaseRpcClient(
            LocalConfig("https://example-ref.supabase.co", "sb_publishable_" + "p" * 36),
            "T" * 43,
            opener=FakeOpener(),
        )
        with self.assertRaises(ValueError):
            client._post("arbitrary_sql", {})

    def test_http_error_is_sanitized(self):
        token = "S" * 43
        error = urllib.error.HTTPError(
            "https://example-ref.supabase.co/rest/v1/rpc/gate2_ping",
            403,
            "Forbidden",
            {},
            None,
        )
        client = SupabaseRpcClient(
            LocalConfig("https://example-ref.supabase.co", "sb_publishable_" + "p" * 36),
            token,
            opener=FakeOpener(error=error),
        )
        with self.assertRaises(SupabaseRpcError) as context:
            client.ping()
        self.assertEqual(context.exception.http_status, 403)
        self.assertNotIn(token, str(context.exception))
        self.assertNotIn("publishable", str(context.exception))


if __name__ == "__main__":
    unittest.main()
