import base64
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from arena_workshop import drive


PNG = drive.PNG_SIGNATURE + b"\x00\x00\x00\x00test-png-payload"


def oauth_config():
    return {
        "client_id": "test-client.apps.googleusercontent.com",
        "project_id": "arena-workshop-test",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "test-secret-never-logged",
        "redirect_uris": ["http://localhost"],
    }


def write_config(path, installed=None, wrapper="installed"):
    body = oauth_config() if installed is None else installed
    path.write_text(json.dumps({wrapper: body}), encoding="utf-8")


class FakeResponse:
    def __init__(self, value, status=200):
        self.data = json.dumps(value).encode("utf-8") if isinstance(value, dict) else value
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]


class OAuthConfigTests(unittest.TestCase):
    def test_accepts_standard_installed_configuration_without_echoing_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "google_oauth_client.json"
            write_config(path)
            config = drive.load_oauth_config(path)
        self.assertEqual(config["client_id"], oauth_config()["client_id"])
        self.assertEqual(config["client_secret"], oauth_config()["client_secret"])

    def test_rejects_web_client_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            write_config(path, wrapper="web")
            with self.assertRaises(drive.DriveError) as caught:
                drive.load_oauth_config(path)
        self.assertEqual(caught.exception.code, "oauth_config_incompatible")
        self.assertNotIn("test-secret", caught.exception.message)

    def test_rejects_non_google_token_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            config = oauth_config()
            config["token_uri"] = "https://attacker.invalid/token"
            write_config(path, installed=config)
            with self.assertRaises(drive.DriveError) as caught:
                drive.load_oauth_config(path)
        self.assertEqual(caught.exception.code, "oauth_config_incompatible")

    def test_rejects_missing_config_with_clear_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(drive.DriveError) as caught:
                drive.load_oauth_config(Path(tmp) / "missing.json")
        self.assertEqual(caught.exception.code, "oauth_config_missing")


class TokenStoreTests(unittest.TestCase):
    def test_default_token_path_uses_local_app_data_outside_repository(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"}):
            path = drive.default_token_path()
        self.assertEqual(path, Path(r"C:\Users\Test\AppData\Local") / "ArenaBlenderWorkshop" / "drive_tokens.dpapi")

    def test_round_trips_only_protected_bytes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.dpapi"
            store = drive.TokenStore(path, protect=lambda value: value[::-1], unprotect=lambda value: value[::-1])
            tokens = {"refresh_token": "refresh-secret", "access_token": "access-secret"}
            store.save(tokens)
            self.assertEqual(store.load(), tokens)
            self.assertNotEqual(path.read_bytes(), json.dumps(tokens, separators=(",", ":"), sort_keys=True).encode("utf-8"))
            self.assertNotIn(b"refresh-secret", path.read_bytes())
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_refuses_plaintext_fallback_when_protection_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            def unavailable(_data):
                raise drive.DriveError("windows_protected_storage_unavailable", "DPAPI required")
            store = drive.TokenStore(Path(tmp) / "tokens.dpapi", protect=unavailable)
            with self.assertRaises(drive.DriveError) as caught:
                store.save({"refresh_token": "secret"})
        self.assertEqual(caught.exception.code, "windows_protected_storage_unavailable")
        self.assertFalse((Path(tmp) / "tokens.dpapi").exists())

    def test_missing_token_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = drive.TokenStore(Path(tmp) / "missing.dpapi", protect=bytes, unprotect=bytes)
            self.assertIsNone(store.load())


class InstalledOAuthTests(unittest.TestCase):
    def test_authorization_uses_pkce_loopback_and_saves_refresh_token(self):
        captured = {}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = drive.TokenStore(
            Path(temporary.name) / "token.bin",
            protect=lambda data: b"enc" + data,
            unprotect=lambda data: data[3:],
        )

        def fake_browser_open(url):
            captured["url"] = url
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            redirect_uri = query["redirect_uri"][0]
            callback_url = redirect_uri + "?" + urllib.parse.urlencode({
                "state": query["state"][0], "code": "temporary-auth-code",
            })

            def send_callback():
                with urllib.request.urlopen(callback_url, timeout=5) as response:
                    captured["callback_status"] = response.status
            thread = threading.Thread(target=send_callback)
            thread.start()
            captured["callback_thread"] = thread
            return True

        def fake_opener(request, timeout=30):
            captured["token_request"] = request
            return FakeResponse({
                "access_token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
                "expires_in": 3600,
                "scope": drive.DRIVE_SCOPE,
            })

        tokens = drive.authorize_installed_app(
            oauth_config(), store, browser_open=fake_browser_open,
            opener=fake_opener, timeout=5,
        )
        captured["callback_thread"].join(timeout=5)
        auth_query = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
        form = urllib.parse.parse_qs(captured["token_request"].data.decode("ascii"))
        verifier = form["code_verifier"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        self.assertEqual(auth_query["scope"], [drive.DRIVE_SCOPE])
        self.assertEqual(auth_query["code_challenge_method"], ["S256"])
        self.assertEqual(auth_query["code_challenge"], [challenge])
        self.assertTrue(auth_query["redirect_uri"][0].startswith("http://127.0.0.1:"))
        self.assertEqual(form["code"], ["temporary-auth-code"])
        self.assertEqual(tokens["refresh_token"], "fake-refresh-token")
        self.assertEqual(store.load()["access_token"], "fake-access-token")
        self.assertEqual(captured["callback_status"], 200)

    def test_consent_denial_is_structured_and_does_not_include_callback_values(self):
        captured = {}
        def fake_browser_open(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            callback_url = query["redirect_uri"][0] + "?" + urllib.parse.urlencode({
                "state": query["state"][0], "error": "access_denied",
            })
            def send_callback():
                urllib.request.urlopen(callback_url, timeout=5).close()
            threading.Thread(target=send_callback).start()
            return True
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = drive.TokenStore(
            Path(temporary.name) / "token.bin",
            protect=lambda data: data, unprotect=lambda data: data,
        )
        with self.assertRaises(drive.DriveError) as caught:
            drive.authorize_installed_app(oauth_config(), store, browser_open=fake_browser_open, timeout=5)
        self.assertEqual(caught.exception.code, "consent_denied")
        self.assertNotIn("access_denied", caught.exception.message)


class DriveApiTests(unittest.TestCase):
    def test_expired_access_token_refreshes_and_preserves_refresh_token(self):
        class Store:
            def __init__(self):
                self.value = {
                    "client_id": oauth_config()["client_id"],
                    "access_token": "expired-access",
                    "refresh_token": "durable-refresh",
                    "expires_at": 900,
                }
            def load(self):
                return dict(self.value)
            def save(self, value):
                self.value = dict(value)
        store = Store()
        captured = {}
        def opener(request, timeout=30):
            captured["request"] = request
            return FakeResponse({"access_token": "fresh-access", "expires_in": 3600})
        api = drive.DriveApi(oauth_config(), store, opener=opener, clock=lambda: 1000)
        tokens = api._tokens()
        form = urllib.parse.parse_qs(captured["request"].data.decode("ascii"))
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["durable-refresh"])
        self.assertEqual(tokens["access_token"], "fresh-access")
        self.assertEqual(tokens["refresh_token"], "durable-refresh")
        self.assertGreater(tokens["expires_at"], 1000)

    def test_missing_authentication_is_reported_without_network_call(self):
        class Store:
            def load(self):
                return None
            def save(self, _value):
                self.fail("unexpected save")
        api = drive.DriveApi(oauth_config(), Store(), opener=lambda *args, **kwargs: self.fail("network"))
        with self.assertRaises(drive.DriveError) as caught:
            api._tokens()
        self.assertEqual(caught.exception.code, "authorization_required")


class DriveApiHttpTests(unittest.TestCase):
    def test_multipart_upload_uses_private_bearer_auth_and_png_payload(self):
        class Store:
            def load(self):
                return {
                    "client_id": oauth_config()["client_id"], "access_token": "fake-bearer",
                    "refresh_token": "fake-refresh", "expires_at": 2000,
                }
            def save(self, _tokens):
                self.fail("unexpected token refresh")
        captured = {}
        def opener(request, timeout=30):
            captured["request"] = request
            return FakeResponse({"id": "drive-file-id", "name": "arena-blender-job-1.png", "mimeType": "image/png"})
        api = drive.DriveApi(oauth_config(), Store(), opener=opener, clock=lambda: 1000)
        metadata = {
            "name": "arena-blender-job-1.png", "mimeType": "image/png",
            "description": "job job-1", "appProperties": {"abwJobKey": "hash"},
        }
        response = api.upload_png(metadata, PNG)
        request = captured["request"]
        self.assertEqual(response["id"], "drive-file-id")
        self.assertIn("/upload/drive/v3/files?", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-bearer")
        self.assertTrue(request.get_header("Content-type").startswith("multipart/related; boundary="))
        self.assertIn(b"image/png", request.data)
        self.assertIn(PNG, request.data)
        self.assertIn(b"job-1", request.data)

    def test_app_property_query_escapes_literal_braces_and_key_value(self):
        api = drive.DriveApi(oauth_config(), token_store=object())
        with patch.object(api, "request_json", return_value={"files": []}) as request_mock:
            files = api.find_by_app_property("job'key\\tail", "id'part\\end", "image/png")
        self.assertEqual(files, [])
        url = request_mock.call_args.args[1]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        self.assertEqual(
            query,
            r"appProperties has { key='job\'key\\tail' and value='id\'part\\end' } and trashed = false and mimeType = 'image/png'",
        )


class FakeDriveApi:
    def __init__(self):
        self.folder = None
        self.files = []
        self.upload_calls = []
        self.create_folder_calls = 0
        self.upload_side_effect = None

    def find_by_app_property(self, key, value, mime_type=None):
        if key == "abwRole" and value == "screenshots-folder":
            return [dict(self.folder)] if self.folder else []
        return [dict(item) for item in self.files if item.get("appProperties", {}).get(key) == value]

    def create_json_file(self, metadata):
        self.create_folder_calls += 1
        self.folder = {"id": "folder-123", **metadata}
        return dict(self.folder)

    def upload_png(self, metadata, data):
        self.upload_calls.append((dict(metadata), data))
        if self.upload_side_effect:
            return self.upload_side_effect(metadata, data)
        item = {
            "id": "file-123", "name": metadata["name"], "mimeType": "image/png",
            "size": str(len(data)), "md5Checksum": hashlib.md5(data).hexdigest(),
            "webViewLink": "https://drive.google.com/file/d/file-123/view",
            "description": metadata["description"], "appProperties": metadata["appProperties"],
        }
        self.files.append(item)
        return dict(item)


class ScreenshotUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "screenshots"
        self.root.mkdir()
        self.job_id = "job-abc123"
        self.path = self.root / (self.job_id + ".png")
        self.path.write_bytes(PNG)
        self.api = FakeDriveApi()
        self.delays = []
        self.uploader = drive.DriveScreenshotUploader(self.api, sleep=self.delays.append, retries=3)

    def test_success_returns_drive_reference_and_job_metadata(self):
        result = self.uploader.upload(self.path, self.job_id)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["file_id"], "file-123")
        self.assertEqual(result["filename"], "arena-blender-{}.png".format(self.job_id))
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(result["retrieval_reference"], "https://drive.google.com/file/d/file-123/view")
        metadata, data = self.api.upload_calls[0]
        self.assertEqual(metadata["appProperties"]["abwRole"], "screenshot")
        self.assertEqual(metadata["description"], "Arena Blender Workshop screenshot for job {}".format(self.job_id))
        self.assertEqual(metadata["name"], result["filename"])
        self.assertEqual(metadata["parents"], ["folder-123"])
        self.assertEqual(data, PNG)
        self.assertEqual(self.api.folder["name"], drive.SCREENSHOT_FOLDER_NAME)

    def test_retry_after_ambiguous_network_failure_recovers_remote_file_without_duplicate(self):
        def fail_after_commit(metadata, data):
            item = {
                "id": "file-after-timeout", "name": metadata["name"], "mimeType": "image/png",
                "size": str(len(data)), "md5Checksum": hashlib.md5(data).hexdigest(),
                "webViewLink": "https://drive.google.com/file/d/recovered/view",
                "appProperties": metadata["appProperties"],
            }
            self.api.files.append(item)
            raise drive.DriveError("network_error", "network", retryable=True)
        self.api.upload_side_effect = fail_after_commit
        result = self.uploader.upload(self.path, self.job_id)
        self.assertEqual(result["status"], "uploaded")
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["file_id"], "file-after-timeout")
        self.assertEqual(len(self.api.upload_calls), 1)
        self.assertEqual(self.delays, [0.25])

    def test_retry_after_transient_failure_then_success(self):
        count = [0]
        def fail_once(metadata, data):
            count[0] += 1
            if count[0] == 1:
                raise drive.DriveError("google_service_unavailable", "temporarily unavailable", retryable=True)
            item = {
                "id": "file-retried", "name": metadata["name"], "mimeType": "image/png",
                "size": str(len(data)), "md5Checksum": hashlib.md5(data).hexdigest(),
                "appProperties": metadata["appProperties"],
            }
            self.api.files.append(item)
            return item
        self.api.upload_side_effect = fail_once
        result = self.uploader.upload(self.path, self.job_id)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(len(self.api.upload_calls), 2)
        self.assertEqual(self.delays, [0.25])

    def test_retries_exhaustion_reports_network_failure_and_keeps_local_png(self):
        original = self.path.read_bytes()
        attempts = [0]
        def fail(metadata, data):
            attempts[0] += 1
            raise drive.DriveError("network_error", "temporary", retryable=True)
        self.api.upload_side_effect = fail
        result = self.uploader.upload(self.path, self.job_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "network_error")
        self.assertEqual(attempts[0], 3)
        self.assertEqual(self.delays, [0.25, 0.5])
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_bytes(), original)

    def test_duplicate_job_upload_returns_existing_file_without_reupload(self):
        first = self.uploader.upload(self.path, self.job_id)
        second = self.uploader.upload(self.path, self.job_id)
        self.assertEqual(first["file_id"], second["file_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.api.upload_calls), 1)

    def test_conflicting_existing_job_is_not_overwritten(self):
        self.api.files.append({
            "id": "other", "name": "old.png", "mimeType": "image/png",
            "size": "1", "md5Checksum": "different",
            "appProperties": {"abwJobKey": hashlib.sha256(self.job_id.encode()).hexdigest()},
        })
        result = self.uploader.upload(self.path, self.job_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "duplicate_job_conflict")
        self.assertEqual(self.api.upload_calls, [])

    def test_rejects_wrong_name_and_non_png_without_upload(self):
        wrong = self.root / "other.png"
        wrong.write_bytes(PNG)
        result = self.uploader.upload(wrong, self.job_id)
        self.assertEqual(result["error"]["code"], "screenshot_job_mismatch")
        self.path.write_bytes(b"not a png")
        result = self.uploader.upload(self.path, self.job_id)
        self.assertEqual(result["error"]["code"], "screenshot_not_png")
        self.assertEqual(self.api.upload_calls, [])

    def test_rejects_oversized_png_and_preserves_local_file(self):
        original_limit = drive.MAX_SCREENSHOT_BYTES
        try:
            drive.MAX_SCREENSHOT_BYTES = 16
            self.path.write_bytes(PNG)
            original = self.path.read_bytes()
            result = self.uploader.upload(self.path, self.job_id)
        finally:
            drive.MAX_SCREENSHOT_BYTES = original_limit
        self.assertEqual(result["error"]["code"], "screenshot_too_large")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.api.upload_calls, [])

    def test_missing_screenshot_is_structured(self):
        result = self.uploader.upload(self.root / (self.job_id + ".png"), "missing-job")
        self.assertEqual(result["error"]["code"], "screenshot_job_mismatch")
        self.path.unlink()
        result = self.uploader.upload(self.path, self.job_id)
        self.assertEqual(result["error"]["code"], "screenshot_missing")

    def test_result_upload_rejects_paths_outside_job_screenshot_folder(self):
        result = {
            "job_id": self.job_id, "status": "completed",
            "result": {"screenshot_path": str(self.path)},
        }
        upload = drive.screenshot_result_upload(
            result, screenshot_root=self.root,
            config_path=Path(self.temp.name) / "missing-client.json",
        )
        self.assertEqual(upload["status"], "failed")
        self.assertEqual(upload["error"]["code"], "oauth_config_missing")
        outside = Path(self.temp.name) / (self.job_id + ".png")
        outside.write_bytes(PNG)
        result["result"]["screenshot_path"] = str(outside)
        upload = drive.screenshot_result_upload(
            result, screenshot_root=self.root,
            config_path=Path(self.temp.name) / "missing-client.json",
        )
        self.assertEqual(upload["error"]["code"], "screenshot_path_mismatch")

    def test_job_id_is_validated(self):
        result = self.uploader.upload(self.path, "../bad")
        self.assertEqual(result["error"]["code"], "invalid_job_id")
        self.assertEqual(self.api.upload_calls, [])


if __name__ == "__main__":
    unittest.main()
