"""Google Drive screenshot upload using a local Windows OAuth identity.

This module intentionally uses only Python's standard library. OAuth tokens are
protected with Windows DPAPI and never stored as plaintext. No Arena connector
credentials are used or accessed here.
"""

import base64
import ctypes
import hashlib
import http.server
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCREENSHOT_FOLDER_NAME = "ArenaBlenderWorkshop Screenshots"
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONFIG_FILENAME = "google_oauth_client.json"
_TOKEN_FILENAME = "drive_tokens.dpapi"
_TOKEN_DIRECTORY = "ArenaBlenderWorkshop"
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class DriveError(RuntimeError):
    """Safe, structured failure. Messages never include credential values."""

    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def default_oauth_config_path():
    """Return the local repository-root config path without reading it."""
    return Path(__file__).resolve().parent.parent / _CONFIG_FILENAME


def default_token_path():
    """Return a user-local token path outside the repository."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / _TOKEN_DIRECTORY / _TOKEN_FILENAME
    return Path.home() / "AppData" / "Local" / _TOKEN_DIRECTORY / _TOKEN_FILENAME


def _is_https_endpoint(value, hostname):
    try:
        parsed = urllib.parse.urlsplit(value)
        return parsed.scheme == "https" and parsed.hostname == hostname and not parsed.username
    except (TypeError, ValueError):
        return False


def load_oauth_config(path=None):
    """Read and validate a Google Desktop/Installed OAuth JSON file safely."""
    path = Path(path) if path is not None else default_oauth_config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DriveError(
            "oauth_config_missing",
            "Google OAuth configuration is missing. Place google_oauth_client.json in the repository root.",
        ) from exc
    except OSError as exc:
        raise DriveError(
            "oauth_config_unreadable",
            "Google OAuth configuration could not be read ({}).".format(type(exc).__name__),
        ) from exc
    try:
        document = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise DriveError("oauth_config_invalid_json", "Google OAuth configuration is not valid JSON.") from exc

    installed = document.get("installed") if isinstance(document, dict) else None
    required = (
        "client_id", "project_id", "auth_uri", "token_uri",
        "auth_provider_x509_cert_url", "client_secret", "redirect_uris",
    )
    if not isinstance(installed, dict) or any(key not in installed for key in required):
        raise DriveError(
            "oauth_config_incompatible",
            "Expected a Google Desktop/Installed OAuth configuration with the required fields.",
        )
    for name in ("client_id", "project_id", "client_secret"):
        if not isinstance(installed.get(name), str) or not installed[name].strip():
            raise DriveError("oauth_config_incompatible", "A required Desktop OAuth field is empty or invalid.")
    if not _is_https_endpoint(installed.get("auth_uri"), "accounts.google.com"):
        raise DriveError("oauth_config_incompatible", "The OAuth authorization endpoint is not Google's HTTPS endpoint.")
    if not _is_https_endpoint(installed.get("token_uri"), "oauth2.googleapis.com"):
        raise DriveError("oauth_config_incompatible", "The OAuth token endpoint is not Google's HTTPS endpoint.")
    if not _is_https_endpoint(installed.get("auth_provider_x509_cert_url"), "www.googleapis.com"):
        raise DriveError("oauth_config_incompatible", "The OAuth certificate endpoint is not Google's HTTPS endpoint.")
    redirects = installed.get("redirect_uris")
    if (not isinstance(redirects, list) or not redirects
            or any(not isinstance(uri, str) for uri in redirects)):
        raise DriveError("oauth_config_incompatible", "Desktop OAuth redirect_uris must be a non-empty list.")
    loopback_found = False
    for uri in redirects:
        try:
            parsed = urllib.parse.urlsplit(uri)
        except ValueError:
            continue
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "[::1]", "::1"}:
            loopback_found = True
            break
    if not loopback_found:
        raise DriveError(
            "oauth_config_incompatible",
            "The Desktop OAuth configuration must include a loopback redirect URI.",
        )
    return dict(installed)


def _dpapi_transform(data, protect):
    """Encrypt/decrypt bytes with Windows DPAPI for the current Windows user."""
    if os.name != "nt":
        raise DriveError(
            "windows_protected_storage_unavailable",
            "Windows DPAPI is required; plaintext token storage is not supported.",
        )
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    input_buffer = ctypes.create_string_buffer(data)
    input_blob = DATA_BLOB(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        function.restype = wintypes.BOOL
        ok = function(ctypes.byref(input_blob), "Arena Blender Workshop", None, None, None,
                      0x1, ctypes.byref(output_blob))
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        function.restype = wintypes.BOOL
        ok = function(ctypes.byref(input_blob), None, None, None, None, 0x1, ctypes.byref(output_blob))
    if not ok:
        raise DriveError(
            "token_protection_failed" if protect else "token_unprotect_failed",
            "Windows could not protect or read the local OAuth token store.",
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def _dpapi_protect(data):
    return _dpapi_transform(data, True)


def _dpapi_unprotect(data):
    return _dpapi_transform(data, False)


class TokenStore:
    """Atomic DPAPI-protected token persistence in the current Windows profile."""

    def __init__(self, path=None, protect=None, unprotect=None):
        self.path = Path(path) if path is not None else default_token_path()
        self._protect = protect or _dpapi_protect
        self._unprotect = unprotect or _dpapi_unprotect

    def save(self, token_data):
        try:
            payload = json.dumps(token_data, separators=(",", ":"), sort_keys=True).encode("utf-8")
            protected = self._protect(payload)
        except DriveError:
            raise
        except Exception as exc:
            raise DriveError("token_protection_failed", "OAuth tokens could not be protected for this Windows user.") from exc
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".drive-token-", suffix=".tmp", dir=str(self.path.parent))
        except OSError as exc:
            raise DriveError("token_store_write_failed", "The protected OAuth token store could not be created.") from exc
        try:
            if os.name != "nt":
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "wb") as stream:
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise DriveError("token_store_write_failed", "The protected OAuth token store could not be written.") from exc
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def load(self):
        try:
            protected = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DriveError("token_store_unreadable", "The local OAuth token store could not be read.") from exc
        try:
            payload = self._unprotect(protected)
            value = json.loads(payload.decode("utf-8"))
        except DriveError:
            raise
        except Exception as exc:
            raise DriveError("token_store_invalid", "The local OAuth token store could not be decrypted.") from exc
        if not isinstance(value, dict) or not isinstance(value.get("refresh_token"), str):
            raise DriveError("token_store_invalid", "The local OAuth token store has an invalid format.")
        return value


class _LoopbackOAuthServer(http.server.HTTPServer):
    def handle_error(self, request, client_address):
        # Do not let HTTP server diagnostics expose an authorization code in a callback URL.
        return


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Default HTTP server logging can include callback query parameters.
        return

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        expected_state = getattr(self.server, "expected_state", None)
        received_state = params.get("state", [None])[0]
        if parsed.path != "/oauth2callback" or not secrets.compare_digest(
                str(received_state or ""), str(expected_state or "")):
            self.server.callback_error = "oauth_state_mismatch"
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authorization response was not accepted. You may close this tab.")
            return
        error = params.get("error", [None])[0]
        if error:
            self.server.callback_error = "consent_denied" if error == "access_denied" else "oauth_callback_error"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Arena Blender Workshop authorization was not completed. You may close this tab.")
            return
        code = params.get("code", [None])[0]
        if not code:
            self.server.callback_error = "oauth_code_missing"
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authorization response was incomplete. You may close this tab.")
            return
        self.server.authorization_code = code
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Arena Blender Workshop authorization received. You may close this tab.")


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _read_json_response(opener, request, timeout=30):
    try:
        response = opener(request, timeout=timeout)
        with response:
            raw = response.read(2 * 1024 * 1024)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(2 * 1024 * 1024)
        except Exception:
            raw = b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DriveError(
            "network_error",
            "A Google authentication or Drive request could not reach Google ({}).".format(type(exc).__name__),
            retryable=True,
        ) from exc
    if status >= 400:
        error_name = None
        try:
            body = json.loads(raw.decode("utf-8"))
            error_name = body.get("error") if isinstance(body, dict) else None
        except Exception:
            pass
        retryable = status in _RETRYABLE_HTTP
        if status == 401:
            code = "unauthorized"
        elif status == 403:
            code = "drive_permission_denied"
        elif status == 404:
            code = "drive_resource_not_found"
        elif retryable:
            code = "google_service_unavailable"
        else:
            code = "google_request_rejected"
        message = "Google rejected the request (HTTP {}).".format(status)
        if error_name == "invalid_grant":
            code = "refresh_token_invalid"
            message = "Google rejected the saved authorization. Run authorize-drive again."
        raise DriveError(code, message, retryable=retryable)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise DriveError("google_response_invalid", "Google returned an unreadable response.") from exc
    if not isinstance(data, dict):
        raise DriveError("google_response_invalid", "Google returned an unexpected response.")
    return data


def _token_expiry(response, now):
    value = response.get("expires_in", 3600)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise DriveError("oauth_token_response_invalid", "Google returned an invalid access-token lifetime.")
    return now + int(value)


def _token_request(config, form_data, opener=urllib.request.urlopen):
    body = urllib.parse.urlencode(form_data).encode("utf-8")
    request = urllib.request.Request(
        config["token_uri"], data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        data = _read_json_response(opener, request)
    except DriveError as exc:
        if exc.code in {"google_request_rejected", "unauthorized", "drive_permission_denied", "drive_resource_not_found"}:
            raise DriveError(
                "oauth_token_exchange_failed",
                "Google did not accept the authorization token request. Check consent and OAuth client configuration.",
            ) from exc
        raise
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise DriveError("oauth_token_response_invalid", "Google did not return a usable access token.")
    return data


def authorize_installed_app(config, token_store, browser_open=None, opener=urllib.request.urlopen, timeout=180):
    """Run a PKCE installed-app authorization on a loopback-only ephemeral port."""
    browser_open = browser_open or webbrowser.open
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(32)
    try:
        server = _LoopbackOAuthServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    except OSError as exc:
        raise DriveError("loopback_listener_failed", "Could not start the local OAuth callback on loopback.") from exc
    server.timeout = 1
    server.expected_state = state
    server.authorization_code = None
    server.callback_error = None
    port = server.server_address[1]
    redirect_uri = "http://127.0.0.1:{}/oauth2callback".format(port)
    query = urllib.parse.urlencode({
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    authorization_url = config["auth_uri"] + ("&" if "?" in config["auth_uri"] else "?") + query
    try:
        try:
            opened = browser_open(authorization_url)
        except Exception as exc:
            raise DriveError("browser_open_failed", "Could not open the Google authorization page in a browser.") from exc
        if opened is False:
            raise DriveError("browser_open_failed", "Could not open the Google authorization page in a browser.")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not server.authorization_code and not server.callback_error:
            server.handle_request()
        if server.callback_error:
            message = (
                "Google access was denied. Re-run authorize-drive and approve the requested Drive access."
                if server.callback_error == "consent_denied"
                else "The Google authorization response could not be validated. Re-run authorize-drive."
            )
            raise DriveError(server.callback_error, message)
        if not server.authorization_code:
            raise DriveError("oauth_callback_timeout", "No Google authorization response arrived before the timeout.")
        form = {
            "grant_type": "authorization_code",
            "code": server.authorization_code,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        token_data = _token_request(config, form, opener=opener)
        refresh_token = token_data.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise DriveError(
                "refresh_token_missing",
                "Google did not issue a refresh token. Re-run authorization and approve offline Drive access.",
            )
        stored = {
            "client_id": config["client_id"],
            "access_token": token_data["access_token"],
            "refresh_token": refresh_token,
            "expires_at": _token_expiry(token_data, time.time()),
            "scope": token_data.get("scope", DRIVE_SCOPE),
        }
        token_store.save(stored)
        return stored
    finally:
        server.server_close()


class DriveApi:
    """Minimal Drive v3 API client with protected local token refresh."""

    def __init__(self, config, token_store, opener=urllib.request.urlopen, clock=time.time):
        self.config = config
        self.token_store = token_store
        self.opener = opener
        self.clock = clock

    def _tokens(self):
        tokens = self.token_store.load()
        if tokens is None:
            raise DriveError("authorization_required", "Run authorize-drive on this Windows PC before uploading.")
        if tokens.get("client_id") != self.config["client_id"]:
            raise DriveError("authorization_client_mismatch", "The saved authorization belongs to a different OAuth client; run authorize-drive again.")
        if not isinstance(tokens.get("access_token"), str) or not isinstance(tokens.get("expires_at"), (int, float)):
            raise DriveError("token_store_invalid", "The local OAuth token store is missing required token data.")
        if tokens["expires_at"] <= self.clock() + 60:
            tokens = self.refresh(tokens)
        return tokens

    def refresh(self, tokens):
        form = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
        }
        response = _token_request(self.config, form, opener=self.opener)
        refreshed = dict(tokens)
        refreshed["access_token"] = response["access_token"]
        refreshed["expires_at"] = _token_expiry(response, self.clock())
        if isinstance(response.get("refresh_token"), str) and response["refresh_token"]:
            refreshed["refresh_token"] = response["refresh_token"]
        if isinstance(response.get("scope"), str):
            refreshed["scope"] = response["scope"]
        self.token_store.save(refreshed)
        return refreshed

    def request_json(self, method, url, body=None, content_type="application/json"):
        refreshed_after_401 = False
        while True:
            tokens = self._tokens()
            headers = {"Authorization": "Bearer " + tokens["access_token"], "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = content_type
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                return _read_json_response(self.opener, request)
            except DriveError as exc:
                if exc.code == "unauthorized" and not refreshed_after_401:
                    self.refresh(tokens)
                    refreshed_after_401 = True
                    continue
                raise

    def find_by_app_property(self, key, value, mime_type=None):
        escaped_key = key.replace("\\", "\\\\").replace("'", "\\'")
        escaped_value = value.replace("\\", "\\\\").replace("'", "\\'")
        query = "appProperties has {{ key='{}' and value='{}' }} and trashed = false".format(
            escaped_key, escaped_value,
        )
        if mime_type:
            query += " and mimeType = '{}'".format(mime_type)
        params = urllib.parse.urlencode({
            "q": query,
            "pageSize": "100",
            "fields": "files(id,name,mimeType,size,md5Checksum,webViewLink,description,appProperties)",
            "spaces": "drive",
        })
        response = self.request_json("GET", "https://www.googleapis.com/drive/v3/files?" + params)
        files = response.get("files", [])
        if not isinstance(files, list):
            raise DriveError("drive_response_invalid", "Google Drive returned an invalid file list.")
        return [item for item in files if isinstance(item, dict) and isinstance(item.get("id"), str)]

    def create_json_file(self, metadata):
        body = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        params = urllib.parse.urlencode({"fields": "id,name,mimeType,webViewLink,description,appProperties"})
        return self.request_json("POST", "https://www.googleapis.com/drive/v3/files?" + params, body)

    def upload_png(self, metadata, png_bytes):
        boundary = "arena-" + secrets.token_hex(16)
        metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        body = (
            b"--" + boundary.encode("ascii") + b"\r\n"
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            + metadata_bytes + b"\r\n--" + boundary.encode("ascii") + b"\r\n"
            b"Content-Type: image/png\r\n\r\n" + png_bytes
            + b"\r\n--" + boundary.encode("ascii") + b"--\r\n"
        )
        params = urllib.parse.urlencode({
            "uploadType": "multipart",
            "fields": "id,name,mimeType,size,md5Checksum,webViewLink,description,appProperties",
        })
        return self.request_json(
            "POST", "https://www.googleapis.com/upload/drive/v3/files?" + params,
            body, "multipart/related; boundary={}".format(boundary),
        )


class DriveScreenshotUploader:
    """Idempotent, bounded uploader for existing job-named PNG screenshots."""

    def __init__(self, api, sleep=time.sleep, retries=3):
        self.api = api
        self.sleep = sleep
        self.retries = max(1, int(retries))

    @staticmethod
    def _validate_screenshot(path, job_id):
        path = Path(path)
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise DriveError("invalid_job_id", "The screenshot job ID is invalid.")
        if path.name != job_id + ".png" or path.suffix.lower() != ".png":
            raise DriveError("screenshot_job_mismatch", "Only the job's original <job_id>.png screenshot can be uploaded.")
        try:
            if not path.is_file():
                raise DriveError("screenshot_missing", "The local screenshot file does not exist.")
            size = path.stat().st_size
            if size <= 0:
                raise DriveError("screenshot_empty", "The local screenshot file is empty.")
            if size > MAX_SCREENSHOT_BYTES:
                raise DriveError("screenshot_too_large", "The screenshot exceeds the 20 MiB upload limit.")
            with path.open("rb") as stream:
                data = stream.read(MAX_SCREENSHOT_BYTES + 1)
            if len(data) > MAX_SCREENSHOT_BYTES:
                raise DriveError("screenshot_too_large", "The screenshot exceeds the 20 MiB upload limit.")
        except DriveError:
            raise
        except OSError as exc:
            raise DriveError("screenshot_unreadable", "The local screenshot could not be read.") from exc
        if not data.startswith(PNG_SIGNATURE):
            raise DriveError("screenshot_not_png", "The local screenshot is not a valid PNG file.")
        return path, data, hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()

    def _get_folder(self):
        folders = self.api.find_by_app_property("abwRole", "screenshots-folder", "application/vnd.google-apps.folder")
        if folders:
            folders.sort(key=lambda item: item.get("id", ""))
            return folders[0]
        metadata = {
            "name": SCREENSHOT_FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
            "description": "Arena Blender Workshop screenshot uploads.",
            "appProperties": {"abwRole": "screenshots-folder"},
        }
        folder = self.api.create_json_file(metadata)
        if not isinstance(folder.get("id"), str) or not folder["id"]:
            raise DriveError("drive_folder_create_failed", "Google Drive did not return a screenshot-folder ID.")
        return folder

    @staticmethod
    def _existing_result(existing, job_id, filename, size, md5, sha256):
        try:
            existing_size = int(existing.get("size", -1))
        except (TypeError, ValueError):
            existing_size = -1
        if existing_size != size or existing.get("md5Checksum") != md5:
            raise DriveError(
                "duplicate_job_conflict",
                "A Drive file already uses this job ID but its contents differ; no duplicate was uploaded.",
            )
        file_id = existing["id"]
        retrieval = existing.get("webViewLink") or "https://drive.google.com/open?id=" + urllib.parse.quote(file_id, safe="")
        return {
            "status": "uploaded",
            "job_id": job_id,
            "file_id": file_id,
            "filename": existing.get("name") or filename,
            "mime_type": existing.get("mimeType") or "image/png",
            "retrieval_reference": retrieval,
            "web_view_link": retrieval,
            "size_bytes": size,
            "sha256": sha256,
            "duplicate": True,
        }

    def upload(self, screenshot_path, job_id):
        filename = "arena-blender-{}.png".format(job_id) if isinstance(job_id, str) else ""
        try:
            path, png_bytes, md5, sha256 = self._validate_screenshot(screenshot_path, job_id)
            filename = "arena-blender-{}.png".format(job_id)
            job_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
            for attempt in range(self.retries):
                try:
                    folder = self._get_folder()
                    if not isinstance(folder.get("id"), str):
                        raise DriveError("drive_folder_create_failed", "The screenshot folder has no usable Drive ID.")
                    existing = self.api.find_by_app_property("abwJobKey", job_key)
                    if existing:
                        return self._existing_result(existing[0], job_id, filename, len(png_bytes), md5, sha256)
                    metadata = {
                        "name": filename,
                        "mimeType": "image/png",
                        "description": "Arena Blender Workshop screenshot for job {}".format(job_id),
                        "parents": [folder["id"]],
                        "appProperties": {"abwJobKey": job_key, "abwRole": "screenshot"},
                    }
                    uploaded = self.api.upload_png(metadata, png_bytes)
                    file_id = uploaded.get("id")
                    if not isinstance(file_id, str) or not file_id:
                        raise DriveError("drive_upload_response_invalid", "Google Drive did not return a file ID.")
                    retrieval = uploaded.get("webViewLink") or "https://drive.google.com/open?id=" + urllib.parse.quote(file_id, safe="")
                    return {
                        "status": "uploaded",
                        "job_id": job_id,
                        "file_id": file_id,
                        "filename": uploaded.get("name") or filename,
                        "mime_type": uploaded.get("mimeType") or "image/png",
                        "retrieval_reference": retrieval,
                        "web_view_link": retrieval,
                        "size_bytes": len(png_bytes),
                        "sha256": sha256,
                        "duplicate": False,
                    }
                except DriveError as exc:
                    if not exc.retryable or attempt + 1 >= self.retries:
                        raise
                    self.sleep(0.25 * (2 ** attempt))
            raise DriveError("drive_upload_failed", "Google Drive upload did not complete.")
        except DriveError as exc:
            return {
                "status": "failed",
                "job_id": job_id if isinstance(job_id, str) else None,
                "file_id": None,
                "filename": filename,
                "mime_type": "image/png",
                "retrieval_reference": None,
                "error": {"code": exc.code, "message": exc.message},
            }


def _token_store_for_path(path=None):
    return TokenStore(path=path)


def build_uploader(config_path=None, token_path=None, opener=urllib.request.urlopen, sleep=time.sleep):
    config = load_oauth_config(config_path)
    store = _token_store_for_path(token_path)
    api = DriveApi(config, store, opener=opener)
    return DriveScreenshotUploader(api, sleep=sleep)


def screenshot_result_upload(result, screenshot_root=None, config_path=None, token_path=None):
    """Upload the screenshot referenced by a completed mailbox result."""
    job_id = result.get("job_id") if isinstance(result, dict) else None
    payload = result.get("result") if isinstance(result, dict) else None
    screenshot_path = payload.get("screenshot_path") if isinstance(payload, dict) else None
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        return _failure_result(job_id, "invalid_job_id", "The job result has an invalid job ID.")
    if not isinstance(screenshot_path, str) or not screenshot_path:
        return _failure_result(job_id, "screenshot_path_missing", "The completed result has no screenshot path.")
    path = Path(screenshot_path)
    if screenshot_root is not None:
        try:
            root = Path(screenshot_root).resolve()
            resolved = path.resolve(strict=True)
            if resolved.parent != root or resolved.name != job_id + ".png":
                return _failure_result(job_id, "screenshot_path_mismatch", "Only this job's screenshot in the local screenshots folder can be uploaded.")
            path = resolved
        except FileNotFoundError:
            return _failure_result(job_id, "screenshot_missing", "The local screenshot file does not exist.")
        except (OSError, RuntimeError):
            return _failure_result(job_id, "screenshot_unreadable", "The local screenshot path could not be verified.")
    try:
        uploader = build_uploader(config_path=config_path, token_path=token_path)
    except DriveError as exc:
        return _failure_result(job_id, exc.code, exc.message)
    return uploader.upload(path, job_id)


def _failure_result(job_id, code, message):
    return {
        "status": "failed",
        "job_id": job_id,
        "file_id": None,
        "filename": ("arena-blender-{}.png".format(job_id)) if isinstance(job_id, str) else None,
        "mime_type": "image/png",
        "retrieval_reference": None,
        "error": {"code": code, "message": message},
    }


def authorization_result(config_path=None, token_path=None, browser_open=None, opener=urllib.request.urlopen, timeout=180):
    """Authorize locally and return a non-sensitive status object."""
    try:
        config = load_oauth_config(config_path)
        store = _token_store_for_path(token_path)
        authorize_installed_app(config, store, browser_open=browser_open, opener=opener, timeout=timeout)
        return {"status": "authorized", "scope": DRIVE_SCOPE, "token_store": str(store.path)}
    except DriveError as exc:
        return {"status": "failed", "error": {"code": exc.code, "message": exc.message}}
