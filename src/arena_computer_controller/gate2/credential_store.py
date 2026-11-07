"""Windows-user-local Gate 2 pairing configuration.

The raw pairing token is protected with Windows DPAPI and is never printed or
written in plaintext. Supabase's publishable/legacy-anon key is intentionally
not treated as a secret; service-role/secret keys are rejected.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .protocol import validate_pairing_token


_CONFIG_VERSION = 1
_TOKEN_DOMAIN = b"ArenaComputerController/Gate2/Pairing/v1:"
_SESSION_TTL_SQL = "2 hours"
_PUBLISHABLE_PREFIX = "sb_publishable_"
_SECRET_PREFIX = "sb_secret_"
_LEGACY_JWT_PREFIX = "eyJ"


class ConfigError(ValueError):
    """Invalid local Gate 2 configuration."""


@dataclass(frozen=True)
class LocalConfig:
    project_url: str
    publishable_key: str = field(repr=False)
    session_id: str | None = None
    protected_pairing_token: str | None = field(default=None, repr=False)

    @property
    def paired(self) -> bool:
        return bool(self.session_id and self.protected_pairing_token)


@dataclass(frozen=True)
class PairingBootstrap:
    session_id: str
    token_hash_hex: str
    bootstrap_sql: str
    revoked_session_id: str | None


def default_config_path() -> Path:
    if os.name != "nt":
        raise ConfigError("Gate 2 configuration is Windows-only.")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ConfigError("LOCALAPPDATA is not available for this Windows user.")
    return Path(local_app_data) / "ArenaComputerController" / "gate2" / "config.json"


def validate_project_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("Supabase project URL is required.")
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host.endswith(".supabase.co")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("Use the HTTPS project URL ending in .supabase.co; do not include a path or credentials.")
    return f"https://{host}"


def validate_public_api_key(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigError("A Supabase publishable key or legacy anon key is required.")
    if value.startswith(_SECRET_PREFIX):
        raise ConfigError("A Supabase secret/service-role key is forbidden in the Windows companion.")
    if value.startswith(_PUBLISHABLE_PREFIX):
        if len(value) < 24:
            raise ConfigError("The publishable key is malformed.")
        return value
    if value.startswith(_LEGACY_JWT_PREFIX):
        parts = value.split(".")
        if len(parts) != 3:
            raise ConfigError("The legacy key is not a valid JWT-shaped anon key.")
        try:
            payload = parts[1] + ("=" * (-len(parts[1]) % 4))
            claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        except Exception:
            raise ConfigError("The legacy key payload could not be checked safely.") from None
        role = claims.get("role") if isinstance(claims, dict) else None
        if role != "anon":
            raise ConfigError("Only a legacy key with the anon role is allowed; service-role keys are forbidden.")
        return value
    raise ConfigError("Only an sb_publishable_ key or a legacy anon JWT is allowed.")


def derive_token_hash_hex(token: str) -> str:
    validate_pairing_token(token)
    return hashlib.sha256(_TOKEN_DOMAIN + token.encode("ascii")).hexdigest()


def _protect_with_dpapi(data: bytes) -> bytes:
    if os.name != "nt":
        raise ConfigError("Windows DPAPI is required to protect the pairing token.")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_buffer = ctypes.create_string_buffer(data, len(data))
    input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = DataBlob()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "ArenaComputerController Gate 2 pairing token",
        None,
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN; current-user scope is the default.
        ctypes.byref(output_blob),
    ):
        raise ConfigError("Windows could not protect the pairing token with DPAPI.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _unprotect_with_dpapi(data: bytes) -> bytes:
    if os.name != "nt":
        raise ConfigError("Windows DPAPI is required to unprotect the pairing token.")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_buffer = ctypes.create_string_buffer(data, len(data))
    input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = DataBlob()
    description = wintypes.LPWSTR()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise ConfigError("Windows could not unprotect the pairing token for this user.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
        if description:
            kernel32.LocalFree(description)


def _validate_session_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise ConfigError("Local session_id is malformed.") from None


def _write_config(path: Path, config: LocalConfig) -> None:
    payload = {
        "schema_version": _CONFIG_VERSION,
        "project_url": validate_project_url(config.project_url),
        "publishable_key": validate_public_api_key(config.publishable_key),
        "session_id": _validate_session_id(config.session_id),
        "protected_pairing_token": config.protected_pairing_token,
    }
    if bool(payload["session_id"]) != bool(payload["protected_pairing_token"]):
        raise ConfigError("Local pairing fields must either both be present or both be absent.")
    encoded = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8") + b"\n"
    temp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="gate2-config-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except OSError:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise ConfigError("The user-local Gate 2 configuration could not be saved.") from None
    except Exception:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise


def configure(path: Path, project_url: str, publishable_key: str) -> None:
    """Write non-secret project details, refusing to silently replace a pairing."""
    project_url = validate_project_url(project_url)
    publishable_key = validate_public_api_key(publishable_key)
    if path.exists():
        current = load_config(path)
        if current.paired:
            raise ConfigError("Unpair the existing test session before changing its project configuration.")
    _write_config(path, LocalConfig(project_url, publishable_key))


def load_config(path: Path) -> LocalConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("Gate 2 is not configured. Run the configure command first.") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("Gate 2 configuration could not be read.") from None
    if not isinstance(raw, dict) or raw.get("schema_version") != _CONFIG_VERSION:
        raise ConfigError("Gate 2 configuration version is unsupported.")
    project_url = validate_project_url(raw.get("project_url"))
    public_key = validate_public_api_key(raw.get("publishable_key"))
    session_id = _validate_session_id(raw.get("session_id"))
    protected = raw.get("protected_pairing_token")
    if protected is not None and not isinstance(protected, str):
        raise ConfigError("Protected pairing material is malformed.")
    if bool(session_id) != bool(protected):
        raise ConfigError("Local pairing fields are incomplete.")
    return LocalConfig(project_url, public_key, session_id, protected)


def load_pairing_token(
    config: LocalConfig,
    *,
    unprotector: Callable[[bytes], bytes] | None = None,
) -> str:
    if not config.paired or config.protected_pairing_token is None:
        raise ConfigError("No local pairing exists. Run the pair command first.")
    try:
        encrypted = base64.b64decode(config.protected_pairing_token, validate=True)
        raw = (unprotector or _unprotect_with_dpapi)(encrypted)
        token = raw.decode("ascii")
        validate_pairing_token(token)
        return token
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("Pairing material could not be opened for this Windows user.") from None


def _bootstrap_sql(session_id: str, token_hash_hex: str, old_session_id: str | None) -> str:
    insert = (
        "INSERT INTO gate2_private.sessions (session_id, token_hash, expires_at)\n"
        "VALUES (\n"
        f"  '{session_id}'::uuid,\n"
        f"  pg_catalog.decode('{token_hash_hex}', 'hex'),\n"
        f"  pg_catalog.transaction_timestamp() + interval '{_SESSION_TTL_SQL}'\n"
        ")\nRETURNING session_id;"
    )
    if not old_session_id:
        return insert
    return (
        "WITH revoked AS (\n"
        "  UPDATE gate2_private.sessions\n"
        "     SET revoked_at = pg_catalog.clock_timestamp()\n"
        f"   WHERE session_id = '{old_session_id}'::uuid AND revoked_at IS NULL\n"
        "   RETURNING session_id\n"
        ")\n"
        + insert
    )


def pair(
    path: Path,
    *,
    protector: Callable[[bytes], bytes] | None = None,
    token_factory: Callable[[], str] | None = None,
    session_id_factory: Callable[[], UUID] | None = None,
) -> PairingBootstrap:
    """Generate and locally protect a token; return only its non-secret hash."""
    current = load_config(path)
    token = (token_factory or (lambda: secrets.token_urlsafe(32)))()
    validate_pairing_token(token)
    session_id = str((session_id_factory or uuid4)())
    session_id = _validate_session_id(session_id)
    assert session_id is not None
    old_session_id = current.session_id
    protected = (protector or _protect_with_dpapi)(token.encode("ascii"))
    if not isinstance(protected, bytes) or not protected:
        raise ConfigError("Pairing token protection returned invalid data.")
    protected_text = base64.b64encode(protected).decode("ascii")
    _write_config(
        path,
        LocalConfig(current.project_url, current.publishable_key, session_id, protected_text),
    )
    token_hash = derive_token_hash_hex(token)
    return PairingBootstrap(
        session_id=session_id,
        token_hash_hex=token_hash,
        bootstrap_sql=_bootstrap_sql(session_id, token_hash, old_session_id),
        revoked_session_id=old_session_id,
    )


def unpair_local(path: Path) -> str | None:
    """Erase local encrypted pairing and return SQL that revokes its DB session."""
    current = load_config(path)
    session_id = current.session_id
    _write_config(path, LocalConfig(current.project_url, current.publishable_key))
    if session_id is None:
        return None
    return (
        "UPDATE gate2_private.sessions SET revoked_at = pg_catalog.clock_timestamp()\n"
        f"WHERE session_id = '{session_id}'::uuid AND revoked_at IS NULL;"
    )
