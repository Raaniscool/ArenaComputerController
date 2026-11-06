"""Provider-neutral command/state layer for the desktop backend."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .errors import ControllerError


_TARGET_ACTIONS = {"click", "type", "clear", "focus", "press_key"}
_DESKTOP_ACTIONS = {"click_at", "scroll_at"}
_WINDOW_ACTIONS = {"focus_window", "press_key"}
_PUBLIC_INPUT_FIELDS = {
    "window_id",
    "application",
    "window_title",
    "name",
    "type",
    "automation_id",
    "available_actions",
    "position",
    "state",
    "enabled",
    "source",
    "confidence",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ComputerController:
    """Owns input-map revisions, action history, and state-change summaries."""

    def __init__(self, backend: Any, *, history_limit: int = 100):
        self.backend = backend
        self._lock = threading.RLock()
        self._revision = 0
        self._inputs: dict[str, dict[str, Any]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=max(1, history_limit))

    def get_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.backend.get_state())

    def get_inputs(self) -> dict[str, Any]:
        """Refresh the live input map and rotate the revision to invalidate old IDs."""
        with self._lock:
            discovery = self.backend.discover_inputs()
            if not isinstance(discovery, dict):
                raise ControllerError("The desktop backend returned an invalid input map.", "backend_error", 500)
            raw_inputs = discovery.get("inputs", [])
            if not isinstance(raw_inputs, list):
                raise ControllerError("The desktop backend returned an invalid input list.", "backend_error", 500)

            self._revision += 1
            revision = self._revision
            new_map: dict[str, dict[str, Any]] = {}
            public_inputs: list[dict[str, Any]] = []
            for index, raw in enumerate(raw_inputs):
                if not isinstance(raw, dict):
                    continue
                stable_key = raw.get("native_key")
                if stable_key is None:
                    stable_key = json.dumps(
                        {key: raw.get(key) for key in sorted(_PUBLIC_INPUT_FIELDS)},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                material = f"{revision}:{index}:{stable_key}".encode("utf-8", errors="replace")
                digest = hashlib.blake2s(material, digest_size=6).hexdigest()
                input_id = f"input-{revision}-{digest}"
                internal = copy.deepcopy(raw)
                internal["id"] = input_id
                internal["revision"] = revision
                new_map[input_id] = internal

                public = {key: copy.deepcopy(raw[key]) for key in _PUBLIC_INPUT_FIELDS if key in raw}
                public["id"] = input_id
                public["revision"] = revision
                public_inputs.append(public)

            self._inputs = new_map
            return {
                "revision": revision,
                "source": discovery.get("source", "unknown"),
                "count": len(public_inputs),
                "inputs": public_inputs,
                "warnings": list(discovery.get("warnings", [])),
            }

    def refresh(self) -> dict[str, Any]:
        """Return a full state snapshot and a freshly revised input map."""
        return {"state": self.get_state(), "input_map": self.get_inputs()}

    def startup_report(self) -> dict[str, Any]:
        state = self.get_state()
        input_map = self.get_inputs()
        try:
            capabilities = self.backend.capabilities()
        except Exception:
            capabilities = {}
        return {"state": state, "input_map": input_map, "capabilities": capabilities}

    def capture_screenshot(self, **options: Any) -> tuple[bytes, dict[str, Any]]:
        return self.backend.capture_screenshot(**options)

    def perform_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ControllerError("Action request must be a JSON object.", "invalid_action", 400)
        action = payload.get("action")
        if not isinstance(action, str) or not action:
            raise ControllerError("Action request requires an 'action' string.", "invalid_action", 400)

        target = payload.get("target")
        window_id = payload.get("window_id")
        history_target = target if isinstance(target, str) else window_id if isinstance(window_id, str) else None
        action_id = str(uuid.uuid4())
        started = time.perf_counter()

        try:
            if action in _TARGET_ACTIONS and target is not None:
                if not isinstance(target, str):
                    raise ControllerError("'target' must be an input identifier string.", "invalid_action", 400)
                if window_id is not None:
                    raise ControllerError("Choose either an input 'target' or a 'window_id', not both.", "invalid_action", 400)
                expected_revision = payload.get("expected_revision")
                if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                    raise ControllerError(
                        "Target actions require the 'expected_revision' returned by GET /v1/inputs.",
                        "revision_required",
                        409,
                    )
                with self._lock:
                    if expected_revision != self._revision:
                        raise ControllerError(
                            "The input map has changed; refresh it before acting.", "stale_input_map", 409
                        )
                    target_record = self._inputs.get(target)
                    if target_record is None:
                        raise ControllerError("Input target is unknown or stale; refresh the input map.", "stale_target", 409)
                    allowed = target_record.get("available_actions", [])
                    if action not in allowed:
                        raise ControllerError(
                            f"Action '{action}' is not available for this input.", "unsupported_action", 400
                        )
                    before = self.backend.get_state()
                    result = self.backend.execute_input(target_record, action, payload)
                    after = self.backend.get_state()
            elif action in _WINDOW_ACTIONS and window_id is not None:
                if not isinstance(window_id, str):
                    raise ControllerError("'window_id' must be a string.", "invalid_action", 400)
                if action == "focus_window" and target is not None:
                    raise ControllerError("focus_window accepts 'window_id', not an input 'target'.", "invalid_action", 400)
                if action == "press_key" and target is not None:
                    raise ControllerError("Choose either an input 'target' or a 'window_id' for press_key.", "invalid_action", 400)
                before = self.backend.get_state()
                result = self.backend.execute_window(window_id, action, payload)
                after = self.backend.get_state()
            elif action in _DESKTOP_ACTIONS and target is None and window_id is None:
                before = self.backend.get_state()
                result = self.backend.execute_desktop(action, payload)
                after = self.backend.get_state()
            elif action == "press_key":
                raise ControllerError("press_key requires an input 'target' or a 'window_id'.", "invalid_action", 400)
            elif action in _TARGET_ACTIONS:
                raise ControllerError("This action requires an input 'target'.", "invalid_action", 400)
            elif action in _DESKTOP_ACTIONS:
                raise ControllerError("Coordinate actions do not accept a target or window_id.", "invalid_action", 400)
            else:
                raise ControllerError(f"Unsupported action: {action}.", "unsupported_action", 400)

            state_changes = self.diff_states(before, after)
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            self._record(
                {
                    "id": action_id,
                    "timestamp": utc_now(),
                    "action": action,
                    "target": history_target,
                    "success": True,
                    "duration_ms": elapsed_ms,
                }
            )
            return {
                "ok": True,
                "action_id": action_id,
                "action": action,
                "result": result,
                "state_changes": state_changes,
                "input_revision": self._revision,
                "input_refresh_recommended": True,
                "duration_ms": elapsed_ms,
            }
        except ControllerError as exc:
            self._record(
                {
                    "id": action_id,
                    "timestamp": utc_now(),
                    "action": action,
                    "target": history_target,
                    "success": False,
                    "error_code": exc.code,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                }
            )
            raise
        except Exception as exc:
            self._record(
                {
                    "id": action_id,
                    "timestamp": utc_now(),
                    "action": action,
                    "target": history_target,
                    "success": False,
                    "error_code": "backend_error",
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                }
            )
            raise ControllerError(
                f"Desktop backend failed ({type(exc).__name__}).", "backend_error", 500
            ) from None

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ControllerError("History limit must be an integer.", "invalid_query", 400)
        limit = max(0, min(limit, 200))
        with self._lock:
            return copy.deepcopy(list(self._history)[-limit:] if limit else [])

    def _record(self, event: dict[str, Any]) -> None:
        with self._lock:
            # Text and key contents are deliberately omitted: they may contain secrets.
            self._history.append(event)

    @staticmethod
    def diff_states(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        old_windows = {item.get("window_id"): item for item in before.get("windows", []) if item.get("window_id")}
        new_windows = {item.get("window_id"): item for item in after.get("windows", []) if item.get("window_id")}

        for window_id in sorted(new_windows.keys() - old_windows.keys()):
            item = new_windows[window_id]
            changes.append({"type": "window_opened", "window_id": window_id, "title": item.get("title", "")})
        for window_id in sorted(old_windows.keys() - new_windows.keys()):
            item = old_windows[window_id]
            changes.append({"type": "window_closed", "window_id": window_id, "title": item.get("title", "")})
        for window_id in sorted(old_windows.keys() & new_windows.keys()):
            old, new = old_windows[window_id], new_windows[window_id]
            if old.get("title") != new.get("title"):
                changes.append(
                    {
                        "type": "window_title_changed",
                        "window_id": window_id,
                        "before": old.get("title", ""),
                        "after": new.get("title", ""),
                    }
                )

        old_active = (before.get("active_window") or {}).get("window_id")
        new_active = (after.get("active_window") or {}).get("window_id")
        if old_active != new_active:
            changes.append(
                {
                    "type": "active_window_changed",
                    "before": old_active,
                    "after": new_active,
                }
            )

        old_apps = {
            item.get("name", "").casefold(): item.get("name", "")
            for item in before.get("running_applications", [])
            if item.get("name")
        }
        new_apps = {
            item.get("name", "").casefold(): item.get("name", "")
            for item in after.get("running_applications", [])
            if item.get("name")
        }
        for key in sorted(new_apps.keys() - old_apps.keys()):
            changes.append({"type": "application_started", "name": new_apps[key]})
        for key in sorted(old_apps.keys() - new_apps.keys()):
            changes.append({"type": "application_stopped", "name": old_apps[key]})
        return changes
