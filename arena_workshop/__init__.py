"""Arena Blender Workshop add-on (Blender 5.2 target).

The add-on is deliberately local-only: it watches a filesystem mailbox and
executes a small allowlist from Blender's main-thread timer. It opens no port.
"""

bl_info = {
    "name": "Arena Blender Workshop MVP",
    "author": "Arena Blender Workshop",
    "version": (0, 4, 0),
    "blender": (5, 2, 0),
    "location": "3D View > Sidebar > Arena Workshop",
    "description": "Process allowlisted Arena jobs from a local file mailbox",
    "category": "System",
}

import logging
from datetime import datetime, timezone
from pathlib import Path

from .mailbox import FileMailbox, default_queue_root
from .operations import (
    OperationError,
    execute_job,
    remove_default_startup_cube,
    resolve_startup_scene,
)

_LOG = logging.getLogger("arena_blender_workshop")
_POLL_SECONDS = 0.5
_bpy = None
_mailbox = None
_startup_scene = None
_registered = False


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _execute(job):
    screenshot_directory = Path(_mailbox.root) / "screenshots" if _mailbox is not None else None
    return execute_job(
        _bpy, job, startup_scene=_startup_scene,
        screenshot_directory=screenshot_directory,
    )


def _failure_response(job, code, message):
    return {
        "protocol_version": job.get("protocol_version", "1.0"),
        "job_id": job.get("job_id", "unknown"),
        "status": "failed",
        "result": None,
        "error": {"code": code, "message": message},
        "completed_at": _utc_now(),
    }


def _poll_mailbox():
    global _registered
    if not _registered or _mailbox is None:
        return None

    try:
        job = _mailbox.claim_next(allowed_workspaces={"test"})
    except Exception:
        _LOG.exception("Mailbox poll failed")
        return _POLL_SECONDS

    if job is None:
        return _POLL_SECONDS

    try:
        response = {
            "protocol_version": job["protocol_version"],
            "job_id": job["job_id"],
            "status": "completed",
            "result": _execute(job),
            "error": None,
            "completed_at": _utc_now(),
        }
    except OperationError as exc:
        _LOG.warning("Job %s failed: %s", job.get("job_id", "unknown"), exc.code)
        response = _failure_response(job, exc.code, exc.message)
    except Exception as exc:
        _LOG.exception("Job %s failed", job.get("job_id", "unknown"))
        response = _failure_response(
            job,
            "blender_operation_failed",
            "Blender operation failed ({}).".format(type(exc).__name__),
        )

    try:
        _mailbox.write_result(response)
    except Exception:
        _LOG.exception("Could not publish result for job %s", job.get("job_id", "unknown"))
    return _POLL_SECONDS


def _initialize_addon():
    """Capture Blender data after registration restrictions have been lifted."""
    global _mailbox, _startup_scene
    if not _registered or _bpy is None:
        return None

    try:
        if _startup_scene is None:
            _startup_scene = resolve_startup_scene(_bpy)
            if _startup_scene is None:
                raise RuntimeError("Arena Blender Workshop could not find a startup scene")
            if remove_default_startup_cube(_bpy, _startup_scene):
                _LOG.info("Removed Blender's untouched startup cube")
        if _mailbox is None:
            _mailbox = FileMailbox(default_queue_root())
        timers = _bpy.app.timers
        if not timers.is_registered(_poll_mailbox):
            timers.register(_poll_mailbox, first_interval=_POLL_SECONDS)
    except Exception:
        _LOG.exception("Arena Blender Workshop initialization is not ready; retrying")
        return _POLL_SECONDS

    _LOG.info("Arena Blender Workshop mailbox active at %s", _mailbox.root)
    return None


def _remove_timers(timers):
    for callback in (_initialize_addon, _poll_mailbox):
        if timers.is_registered(callback):
            timers.unregister(callback)


def register():
    global _bpy, _mailbox, _startup_scene, _registered
    import bpy

    if _registered:
        return

    timers = bpy.app.timers
    _remove_timers(timers)

    _bpy = bpy
    _mailbox = None
    _startup_scene = None
    _registered = True
    try:
        timers.register(_initialize_addon, first_interval=_POLL_SECONDS)
    except Exception:
        _remove_timers(timers)
        _registered = False
        _mailbox = None
        _startup_scene = None
        _bpy = None
        raise


def unregister():
    global _registered, _mailbox, _startup_scene, _bpy
    bpy = _bpy
    _registered = False
    try:
        if bpy is not None:
            _remove_timers(bpy.app.timers)
    finally:
        _mailbox = None
        _startup_scene = None
        _bpy = None
