from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from arena_computer_controller.gate2.credential_store import (
    ConfigError,
    LocalConfig,
    configure,
    derive_token_hash_hex,
    load_config,
    load_pairing_token,
    pair,
    unpair_local,
    validate_project_url,
    validate_public_api_key,
)


PUBLISHABLE_KEY = "sb_publishable_" + "p" * 36
TEST_TOKEN = "A" * 43


def legacy_key(role: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + payload + ".signature"


class Gate2CredentialTests(unittest.TestCase):
    def test_only_publishable_or_anon_keys_are_accepted(self) -> None:
        self.assertEqual(validate_public_api_key(PUBLISHABLE_KEY), PUBLISHABLE_KEY)
        self.assertEqual(validate_public_api_key(legacy_key("anon")), legacy_key("anon"))
        with self.assertRaises(ConfigError):
            validate_public_api_key("sb_secret_" + "s" * 40)
        with self.assertRaises(ConfigError):
            validate_public_api_key(legacy_key("service_role"))
        with self.assertRaises(ConfigError):
            validate_public_api_key("not-a-supabase-key")

    def test_project_url_is_https_project_host_only(self) -> None:
        self.assertEqual(validate_project_url("https://example-ref.supabase.co/"), "https://example-ref.supabase.co")
        for url in (
            "http://example-ref.supabase.co",
            "https://evil.example.com",
            "https://user:pass@example-ref.supabase.co",
            "https://example-ref.supabase.co/rest/v1",
        ):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                validate_project_url(url)

    def test_pairing_stores_only_dpapi_ciphertext_and_prints_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            configure(path, "https://example-ref.supabase.co", PUBLISHABLE_KEY)
            previous = pair(
                path,
                protector=lambda raw: b"dpapi:" + raw,
                token_factory=lambda: TEST_TOKEN,
                session_id_factory=lambda: UUID("11111111-1111-4111-8111-111111111111"),
            )
            on_disk = path.read_text(encoding="utf-8")
            config = load_config(path)
            token = load_pairing_token(config, unprotector=lambda raw: raw.removeprefix(b"dpapi:"))

            self.assertEqual(token, TEST_TOKEN)
            self.assertNotIn(TEST_TOKEN, on_disk)
            self.assertNotIn(TEST_TOKEN, previous.bootstrap_sql)
            self.assertIn(derive_token_hash_hex(TEST_TOKEN), previous.bootstrap_sql)
            self.assertEqual(previous.token_hash_hex, derive_token_hash_hex(TEST_TOKEN))
            self.assertIn("gate2_private.sessions", previous.bootstrap_sql)
            self.assertEqual(previous.session_id, config.session_id)
            self.assertEqual(previous.revoked_session_id, None)
            self.assertNotIn("sb_secret_", previous.bootstrap_sql)

    def test_pair_rotation_revokes_the_previous_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            configure(path, "https://example-ref.supabase.co", PUBLISHABLE_KEY)
            first = pair(
                path,
                protector=lambda raw: b"x" + raw,
                token_factory=lambda: "A" * 43,
                session_id_factory=lambda: UUID("11111111-1111-4111-8111-111111111111"),
            )
            second = pair(
                path,
                protector=lambda raw: b"y" + raw,
                token_factory=lambda: "B" * 43,
                session_id_factory=lambda: UUID("22222222-2222-4222-8222-222222222222"),
            )
            self.assertEqual(second.revoked_session_id, first.session_id)
            self.assertIn(first.session_id, second.bootstrap_sql)
            self.assertIn("revoked_at", second.bootstrap_sql)
            self.assertNotIn("A" * 43, second.bootstrap_sql)
            config = load_config(path)
            self.assertEqual(load_pairing_token(config, unprotector=lambda raw: raw[1:]), "B" * 43)

    def test_unpair_erases_local_pairing_and_returns_revoke_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            configure(path, "https://example-ref.supabase.co", PUBLISHABLE_KEY)
            paired = pair(
                path,
                protector=lambda raw: b"x" + raw,
                token_factory=lambda: TEST_TOKEN,
                session_id_factory=lambda: UUID("33333333-3333-4333-8333-333333333333"),
            )
            sql = unpair_local(path)
            self.assertIn(paired.session_id, sql)
            self.assertIn("revoked_at", sql)
            config = load_config(path)
            self.assertFalse(config.paired)
            self.assertNotIn(TEST_TOKEN, path.read_text(encoding="utf-8"))

    def test_config_repr_does_not_show_keys_or_pairing_blob(self) -> None:
        config = LocalConfig("https://example-ref.supabase.co", PUBLISHABLE_KEY, None, "encrypted-value")
        self.assertNotIn(PUBLISHABLE_KEY, repr(config))
        self.assertNotIn("encrypted-value", repr(config))


if __name__ == "__main__":
    unittest.main()
