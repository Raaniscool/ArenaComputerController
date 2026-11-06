"""Thin adapter for the existing ArenaBlenderWorkshop file-mailbox protocol.

The Blender connector remains a separate checkout. Configure its repository root
with ARENA_BLENDER_WORKSHOP_PATH; this adapter imports its protocol/mailbox code
and does not reimplement or vendor Blender operations.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..errors import ControllerError

_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_TIMEOUT_SECONDS = 540.0


def _load_connector_modules(connector_root: str | os.PathLike[str]) -> SimpleNamespace:
    """Load the upstream package under a private name to avoid module collisions."""
    root = Path(connector_root).expanduser().resolve()
    package_dir = root / "arena_workshop"
    init_file = package_dir / "__init__.py"
    if not init_file.is_file():
        raise ControllerError(
            "The configured Blender connector path must contain arena_workshop/__init__.py.",
            "blender_connector_not_found",
            503,
        )

    namespace = "_arena_workshop_" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    try:
        if namespace not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                namespace,
                init_file,
                submodule_search_locations=[str(package_dir)],
            )
            if spec is None or spec.loader is None:
                raise ImportError("could not create a package loader")
            package = importlib.util.module_from_spec(spec)
            sys.modules[namespace] = package
            spec.loader.exec_module(package)

        mailbox_module = importlib.import_module(namespace + ".mailbox")
        local_smoke_module = importlib.import_module(namespace + ".local_smoke")
        protocol_module = importlib.import_module(namespace + ".protocol")
        return SimpleNamespace(
            FileMailbox=mailbox_module.FileMailbox,
            default_queue_root=mailbox_module.default_queue_root,
            create_local_job=local_smoke_module.create_local_job,
            wait_for_result=local_smoke_module.wait_for_result,
            validate_job=protocol_module.validate_job,
            JobValidationError=protocol_module.JobValidationError,
            SUPPORTED_OPERATIONS=protocol_module.SUPPORTED_OPERATIONS,
        )
    except ControllerError:
        raise
    except Exception as exc:
        raise ControllerError(
            f"Could not load ArenaBlenderWorkshop ({type(exc).__name__}).",
            "blender_connector_load_failed",
            503,
        ) from None


class BlenderMailboxAdapter:
    """Submit allowlisted Blender jobs and retrieve their local results/images."""

    def __init__(
        self,
        connector_root: str | os.PathLike[str] | None = None,
        *,
        mailbox: Any | None = None,
        connector_modules: Any | None = None,
        waiter: Any | None = None,
    ):
        if connector_modules is None:
            connector_root = connector_root or os.environ.get("ARENA_BLENDER_WORKSHOP_PATH")
            if not connector_root:
                raise ControllerError(
                    "Set ARENA_BLENDER_WORKSHOP_PATH to the ArenaBlenderWorkshop checkout.",
                    "blender_integration_not_configured",
                    503,
                )
            self.connector_root = Path(connector_root).expanduser().resolve()
            connector_modules = _load_connector_modules(self.connector_root)
        else:
            self.connector_root = Path(connector_root).expanduser().resolve() if connector_root else None

        self._connector = connector_modules
        self.mailbox = mailbox or connector_modules.FileMailbox(connector_modules.default_queue_root())
        self._waiter = waiter or connector_modules.wait_for_result
        self._lock = threading.Lock()
        self._request_root = Path(self.mailbox.root) / "controller_requests"

    @classmethod
    def from_environment(cls) -> "BlenderMailboxAdapter":
        return cls(os.environ.get("ARENA_BLENDER_WORKSHOP_PATH"))

    def execute(
        self,
        operation: str,
        parameters: dict[str, Any] | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        timeout = self._validate_timeout(timeout)
        if parameters is not None and not isinstance(parameters, dict):
            raise ControllerError("Blender command 'parameters' must be an object.", "invalid_blender_command", 400)
        if not isinstance(operation, str) or operation not in self._connector.SUPPORTED_OPERATIONS:
            raise ControllerError("The Blender operation is not allowlisted.", "unsupported_blender_operation", 400)
        request_hash = self._request_hash(request_id)
        started = time.perf_counter()
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            return self._timeout_result(operation, None, started, "blender_busy_timeout")

        try:
            try:
                job = self._connector.create_local_job(operation, parameters)
                if not isinstance(job, dict):
                    raise ControllerError("Blender connector created an invalid job.", "blender_protocol_error", 502)
                if request_hash is not None:
                    job["job_id"] = "controller-" + request_hash
                    # Re-run the connector's validator after substituting the stable id.
                    job = self._connector.validate_job(job, {"test"})
                job_id = job.get("job_id")
                if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
                    raise ControllerError("Blender connector created an invalid job ID.", "blender_protocol_error", 502)
                fingerprint = self._fingerprint(operation, job.get("parameters"))
            except ControllerError:
                raise
            except Exception as exc:
                validation_error = getattr(self._connector, "JobValidationError", ())
                if validation_error and isinstance(exc, validation_error):
                    raise ControllerError(
                        f"Blender command validation failed ({exc}).", "invalid_blender_command", 400
                    ) from None
                raise ControllerError(
                    f"Could not prepare the Blender job ({type(exc).__name__}).", "blender_protocol_error", 400
                ) from None

            record_path = self._record_path(job_id)
            record = self._read_record(record_path)
            duplicate = False
            mailbox_result = None

            if record is not None:
                if record.get("fingerprint") != fingerprint or record.get("operation") != operation:
                    raise ControllerError(
                        "request_id was already used for a different Blender command.",
                        "idempotency_conflict",
                        409,
                    )
                cached_result = record.get("cached_result")
                if isinstance(cached_result, dict):
                    return self._format_result(
                        cached_result, operation, job_id, duplicate=True, started=started
                    )
                try:
                    mailbox_result = self.mailbox.read_result(job_id)
                except Exception:
                    mailbox_result = None
                if mailbox_result is not None:
                    self._cache_result(record_path, record, mailbox_result)
                    return self._format_result(
                        mailbox_result, operation, job_id, duplicate=True, started=started
                    )
                existing = self._existing_job_state(job_id, operation, fingerprint)
                if existing == "rejected":
                    return self._rejected_result(operation, job_id, started, duplicate=True)
                if existing != "matching":
                    raise ControllerError(
                        "The prior Blender request has no result or live mailbox record; refusing to enqueue it again.",
                        "blender_job_state_ambiguous",
                        409,
                    )
                duplicate = True
            else:
                existing = self._existing_job_state(job_id, operation, fingerprint)
                if existing == "rejected":
                    return self._rejected_result(operation, job_id, started, duplicate=True)
                if existing is not None and existing != "matching":
                    raise ControllerError(
                        "A mailbox job with this ID exists but cannot be verified against the request.",
                        "idempotency_conflict",
                        409,
                    )

                record = {
                    "job_id": job_id,
                    "operation": operation,
                    "fingerprint": fingerprint,
                    "request_id_hash": request_hash,
                    "status": "dispatching",
                    "cached_result": None,
                }
                # Persist intent before enqueue so a controller crash cannot turn a retry
                # into a second modeling action if the mailbox later removes its job files.
                self._write_record(record_path, record)
                if existing == "matching":
                    duplicate = True
                else:
                    try:
                        self.mailbox.enqueue(job, allowed_workspaces={"test"})
                    except FileExistsError:
                        existing = self._existing_job_state(job_id, operation, fingerprint)
                        if existing == "rejected":
                            return self._rejected_result(operation, job_id, started, duplicate=True)
                        if existing != "matching":
                            raise ControllerError(
                                "The Blender mailbox already contains this job ID with unknown contents.",
                                "idempotency_conflict",
                                409,
                            ) from None
                        duplicate = True
                    except Exception as exc:
                        raise ControllerError(
                            f"Could not enqueue the Blender job ({type(exc).__name__}).",
                            "blender_mailbox_error",
                            503,
                        ) from None
                record["status"] = "queued"
                self._write_record(record_path, record)

            if mailbox_result is None:
                try:
                    mailbox_result = self.mailbox.read_result(job_id)
                except Exception:
                    mailbox_result = None
            if mailbox_result is None:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    return self._timeout_result(operation, job_id, started, duplicate=duplicate)
                try:
                    mailbox_result = self._waiter(self.mailbox, job_id, remaining)
                except Exception as exc:
                    raise ControllerError(
                        f"Waiting for Blender failed ({type(exc).__name__}).",
                        "blender_mailbox_error",
                        503,
                    ) from None

            if mailbox_result is None:
                if self._rejected_path(job_id).exists():
                    return self._rejected_result(operation, job_id, started, duplicate=duplicate)
                return self._timeout_result(operation, job_id, started, duplicate=duplicate)
            if not isinstance(mailbox_result, dict):
                raise ControllerError("Blender returned a malformed result.", "blender_result_invalid", 502)
            if mailbox_result.get("job_id") != job_id:
                raise ControllerError("Blender result job ID does not match the request.", "blender_result_invalid", 502)
            if mailbox_result.get("status") not in {"completed", "failed"}:
                raise ControllerError("Blender returned an unknown result status.", "blender_result_invalid", 502)

            record = record or {
                "job_id": job_id,
                "operation": operation,
                "fingerprint": fingerprint,
                "request_id_hash": request_hash,
                "status": "queued",
                "cached_result": None,
            }
            self._cache_result(record_path, record, mailbox_result)
            return self._format_result(mailbox_result, operation, job_id, duplicate, started)
        finally:
            self._lock.release()

    def probe(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Use the connector's harmless hello_test job as its liveness probe."""
        result = self.execute(
            "hello_test",
            None,
            timeout=timeout,
        )
        responding = result.get("status") in {"completed", "failed"}
        operation_result = result.get("result") if isinstance(result.get("result"), dict) else {}
        return {
            "configured": True,
            "addon_responding": responding,
            "healthy": result.get("status") == "completed",
            "blender_version": operation_result.get("blender_version"),
            "active_scene": operation_result.get("active_scene"),
            "job": result,
        }

    def read_screenshot(self, job_id: str) -> tuple[bytes, dict[str, Any]]:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ControllerError("Invalid Blender job ID.", "invalid_job_id", 400)
        record = self._read_record(self._record_path(job_id))
        if record is None or record.get("operation") != "capture_screenshot":
            raise ControllerError("No controller screenshot job is known for this ID.", "screenshot_job_not_found", 404)

        result = None
        try:
            result = self.mailbox.read_result(job_id)
        except Exception:
            result = None
        if not isinstance(result, dict):
            result = record.get("cached_result")
        if not isinstance(result, dict) or result.get("job_id") != job_id:
            raise ControllerError("The Blender screenshot result does not match this job.", "blender_result_invalid", 502)
        if result.get("status") != "completed":
            raise ControllerError("The Blender screenshot job has no completed result.", "screenshot_not_ready", 409)
        operation_result = result.get("result")
        screenshot_path = operation_result.get("screenshot_path") if isinstance(operation_result, dict) else None
        if not isinstance(screenshot_path, str) or not screenshot_path:
            raise ControllerError("Blender result does not contain a screenshot path.", "screenshot_missing", 404)

        supplied_path = self._confined_screenshot_path(job_id, screenshot_path)
        if supplied_path is None:
            raise ControllerError("Blender returned a PNG outside the job screenshot directory.", "screenshot_path_rejected", 403)
        try:
            if supplied_path.is_symlink():
                raise ControllerError("The Blender PNG must not be a symbolic link.", "screenshot_path_rejected", 403)
            size = supplied_path.stat().st_size
            if size <= 0 or size > _MAX_SCREENSHOT_BYTES:
                raise ControllerError("The Blender PNG is empty or exceeds 20 MiB.", "screenshot_size_invalid", 413)
            data = supplied_path.read_bytes()
        except ControllerError:
            raise
        except OSError:
            raise ControllerError("The Blender PNG could not be read.", "screenshot_missing", 404) from None
        if not data.startswith(_PNG_SIGNATURE):
            raise ControllerError("The Blender screenshot is not a valid PNG payload.", "screenshot_invalid", 502)
        digest = hashlib.sha256(data).hexdigest()
        declared_digest = operation_result.get("sha256")
        if declared_digest and declared_digest != digest:
            raise ControllerError("The Blender PNG hash does not match its result metadata.", "screenshot_integrity_error", 502)
        width = operation_result.get("width")
        height = operation_result.get("height")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or not 64 <= width <= 1280
            or isinstance(height, bool)
            or not isinstance(height, int)
            or not 64 <= height <= 720
        ):
            raise ControllerError("Blender screenshot dimensions are invalid.", "screenshot_metadata_invalid", 502)
        metadata = {
            "job_id": job_id,
            "mime_type": "image/png",
            "format": "PNG",
            "capture_type": "camera_render",
            "width": width,
            "height": height,
            "bytes": len(data),
            "sha256": digest,
        }
        return data, metadata

    @staticmethod
    def _validate_timeout(timeout: Any) -> float:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ControllerError("Blender timeout must be a number of seconds.", "invalid_timeout", 400)
        value = float(timeout)
        if not math.isfinite(value) or not 0.1 <= value <= _MAX_TIMEOUT_SECONDS:
            raise ControllerError(
                f"Blender timeout must be between 0.1 and {_MAX_TIMEOUT_SECONDS:g} seconds.",
                "invalid_timeout",
                400,
            )
        return value

    @staticmethod
    def _request_hash(request_id: str | None) -> str | None:
        if request_id is None:
            return None
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 512:
            raise ControllerError("request_id must be a non-empty string up to 512 characters.", "invalid_request_id", 400)
        try:
            encoded = request_id.encode("utf-8")
        except UnicodeEncodeError:
            raise ControllerError("request_id must contain valid Unicode text.", "invalid_request_id", 400) from None
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _fingerprint(operation: str, parameters: dict[str, Any] | None) -> str:
        try:
            payload = json.dumps(
                {"operation": operation, "parameters": parameters},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ControllerError("Blender parameters must be finite JSON values.", "invalid_blender_command", 400) from None
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _record_path(self, job_id: str) -> Path:
        return self._request_root / (job_id + ".json")

    def _rejected_path(self, job_id: str) -> Path:
        return self.mailbox.rejected / (job_id + ".json.invalid")

    def _read_record(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError):
            raise ControllerError("Controller request history is unreadable.", "blender_request_store_error", 500) from None
        return value if isinstance(value, dict) else None

    def _write_record(self, path: Path, value: dict[str, Any]) -> None:
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".blender-request-", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            raise ControllerError("Controller request history could not be written.", "blender_request_store_error", 503) from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _cache_result(self, path: Path, record: dict[str, Any], result: dict[str, Any]) -> None:
        updated = copy.deepcopy(record)
        updated["status"] = result.get("status")
        updated["cached_result"] = copy.deepcopy(result)
        self._write_record(path, updated)

    def _existing_job_state(
        self,
        job_id: str,
        operation: str,
        fingerprint: str,
    ) -> str | None:
        for directory in (self.mailbox.inbox, self.mailbox.processing):
            job_path = directory / (job_id + ".json")
            if not job_path.is_file():
                continue
            try:
                existing_job = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                return "conflict"
            if not isinstance(existing_job, dict):
                return "conflict"
            existing_fingerprint = self._fingerprint(
                existing_job.get("operation"), existing_job.get("parameters")
            ) if isinstance(existing_job.get("operation"), str) else None
            if (
                existing_job.get("protocol_version") == "1.0"
                and existing_job.get("job_id") == job_id
                and existing_job.get("workspace_id") == "test"
                and existing_job.get("operation") == operation
                and existing_fingerprint == fingerprint
            ):
                return "matching"
            return "conflict"
        if self._rejected_path(job_id).exists():
            return "rejected"
        try:
            if self.mailbox.read_result(job_id) is not None:
                return "result_unknown"
        except Exception:
            return "conflict"
        return None

    def _rejected_result(self, operation: str, job_id: str, started: float, duplicate: bool) -> dict[str, Any]:
        return {
            "ok": False,
            "job_id": job_id,
            "operation": operation,
            "status": "failed",
            "result": None,
            "error": {
                "code": "blender_job_rejected",
                "message": "The Blender add-on rejected the mailbox job; it was not resubmitted.",
            },
            "duplicate_request": duplicate,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    def _timeout_result(
        self,
        operation: str,
        job_id: str | None,
        started: float,
        code: str = "blender_timeout",
        duplicate: bool = False,
    ) -> dict[str, Any]:
        message = (
            "Another Blender command remained active beyond this request's timeout."
            if code == "blender_busy_timeout"
            else "No Blender result arrived before the timeout; the queued job was not resubmitted."
        )
        return {
            "ok": False,
            "job_id": job_id,
            "operation": operation,
            "status": "timeout",
            "result": None,
            "error": {"code": code, "message": message},
            "duplicate_request": duplicate,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    def _confined_screenshot_path(self, job_id: str, path_value: str) -> Path | None:
        if not isinstance(path_value, str) or not path_value:
            return None
        expected = Path(self.mailbox.root).resolve() / "screenshots" / (job_id + ".png")
        try:
            supplied = Path(path_value)
            if not supplied.is_absolute():
                return None
            if os.path.normcase(os.path.abspath(str(supplied))) != os.path.normcase(os.path.abspath(str(expected))):
                return None
            if expected.parent.is_symlink():
                return None
            return expected
        except (OSError, ValueError):
            return None

    def _screenshot_is_available(self, job_id: str, operation_result: dict[str, Any]) -> bool:
        expected = self._confined_screenshot_path(job_id, operation_result.get("screenshot_path"))
        if expected is None or expected.is_symlink():
            return False
        try:
            size = expected.stat().st_size
            if size <= len(_PNG_SIGNATURE) or size > _MAX_SCREENSHOT_BYTES:
                return False
            with expected.open("rb") as stream:
                return stream.read(len(_PNG_SIGNATURE)) == _PNG_SIGNATURE
        except OSError:
            return False

    def _format_result(
        self,
        mailbox_result: dict[str, Any],
        operation: str,
        job_id: str,
        duplicate: bool,
        started: float,
    ) -> dict[str, Any]:
        operation_result = copy.deepcopy(mailbox_result.get("result"))
        if operation == "capture_screenshot" and isinstance(operation_result, dict):
            screenshot_available = self._screenshot_is_available(job_id, operation_result)
            operation_result.pop("screenshot_path", None)
            operation_result["screenshot_available"] = screenshot_available
            if screenshot_available:
                operation_result["screenshot_url"] = f"/v1/integrations/blender/jobs/{job_id}/screenshot"
        error = copy.deepcopy(mailbox_result.get("error"))
        if mailbox_result.get("status") == "failed" and not isinstance(error, dict):
            error = {
                "code": "blender_operation_failed",
                "message": "Blender reported a failed operation without structured error details.",
            }
        return {
            "ok": mailbox_result.get("status") == "completed",
            "job_id": job_id,
            "operation": operation,
            "status": mailbox_result.get("status"),
            "result": operation_result,
            "error": error,
            "duplicate_request": duplicate,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
