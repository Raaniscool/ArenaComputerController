"""Native desktop operations used by the small HTTP controller.

The first implementation deliberately keeps platform-specific code behind one
backend. Windows uses Win32 for window discovery, UI Automation through
pywinauto for structured controls, and MSS/Pillow for on-demand screenshots.
"""

from __future__ import annotations

import csv
import ctypes
import datetime as _datetime
import importlib.util
import io
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .errors import ControllerError


def _now() -> str:
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, name)
        return value() if callable(value) and name.startswith("get_") else value
    except Exception:
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


class SystemDesktopBackend:
    """Desktop implementation with a Windows UIA path and safe fallbacks."""

    _MAX_PROCESSES = 2000
    _INTERACTIVE_TYPES = {
        "Button",
        "CheckBox",
        "ComboBox",
        "Hyperlink",
        "ListItem",
        "MenuItem",
        "RadioButton",
        "Slider",
        "SplitButton",
        "TabItem",
        "ToggleButton",
        "TreeItem",
    }

    def capabilities(self) -> dict[str, Any]:
        windows = os.name == "nt"
        return {
            "platform": platform.platform(),
            "platform_supported": windows,
            "window_discovery": windows,
            "ui_automation": windows and _has_module("pywinauto"),
            "screenshots": windows and _has_module("PIL"),
            "coordinate_actions": windows and _has_module("pyautogui"),
            "notes": []
            if windows
            else ["This prototype controls Windows; non-Windows hosts expose process-only discovery."],
        }

    def get_state(self) -> dict[str, Any]:
        processes = self._list_processes()
        if os.name == "nt":
            windows = self._windows(processes)
        else:
            windows = []

        window_pids = {item["process_id"] for item in windows}
        grouped: dict[str, dict[str, Any]] = {}
        for process in processes:
            name = process["name"] or "Unknown"
            key = name.casefold()
            if key not in grouped:
                grouped[key] = {
                    "name": name,
                    "process_count": 0,
                    "has_window": False,
                }
            grouped[key]["process_count"] += 1
            if process["pid"] in window_pids:
                grouped[key]["has_window"] = True

        running_apps = sorted(grouped.values(), key=lambda item: item["name"].casefold())
        app_names = {item["name"].casefold() for item in running_apps}
        integrations = self._integration_status(app_names)
        active = next((item for item in windows if item["is_active"]), None)
        process_limit = self._MAX_PROCESSES

        return {
            "timestamp": _now(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
            },
            "platform_supported": os.name == "nt",
            "active_window": active,
            "windows": windows,
            "running_applications": running_apps,
            "running_processes": processes[:process_limit],
            "running_processes_truncated": len(processes) > process_limit,
            "integrations": integrations,
        }

    def discover_inputs(self) -> dict[str, Any]:
        if os.name != "nt":
            return {
                "source": "unavailable",
                "inputs": [],
                "warnings": ["UI Automation input discovery is currently implemented for Windows only."],
            }

        try:
            from pywinauto import Desktop
        except ImportError:
            return {
                "source": "windows_uia",
                "inputs": [],
                "warnings": [
                    "pywinauto is not installed. Install the Windows extras with: "
                    "python -m pip install -e '.[windows]'"
                ],
            }

        state = self.get_state()
        window_by_handle = {item["handle"]: item for item in state["windows"]}
        discovered: list[dict[str, Any]] = []
        warnings: list[str] = []
        maximum = _as_int(os.environ.get("ARENA_CONTROLLER_MAX_INPUTS", "2500"), 2500)
        maximum = max(1, min(maximum, 20000))

        try:
            desktop = Desktop(backend="uia")
            top_level_windows = desktop.windows(visible_only=True)
        except Exception as exc:
            return {
                "source": "windows_uia",
                "inputs": [],
                "warnings": [f"Could not enumerate UI Automation windows ({type(exc).__name__})."],
            }

        for top_window in top_level_windows:
            if len(discovered) >= maximum:
                warnings.append(f"Input discovery stopped at the configured limit of {maximum} controls.")
                break
            hwnd = _as_int(_safe_attr(top_window, "handle"), 0)
            state_window = window_by_handle.get(hwnd)
            if not hwnd or state_window is None:
                continue
            window_id = state_window["window_id"]

            try:
                controls = top_window.descendants()
            except Exception as exc:
                warnings.append(
                    f"Could not inspect window {state_window['title']!r} ({type(exc).__name__})."
                )
                continue

            occurrences: dict[str, int] = {}
            for control in controls:
                try:
                    info = control.element_info
                    base_key = self._ui_base_key(hwnd, info)
                    occurrence = occurrences.get(base_key, 0)
                    occurrences[base_key] = occurrence + 1
                    native_key = json.dumps(
                        [base_key, occurrence], ensure_ascii=False, separators=(",", ":")
                    )

                    control_type = str(_safe_attr(info, "control_type", "") or "")
                    if control_type not in self._INTERACTIVE_TYPES and control_type != "Edit":
                        continue
                    visible = bool(_safe_attr(info, "is_visible", True))
                    offscreen = bool(_safe_attr(info, "is_offscreen", False))
                    if not visible or offscreen:
                        continue

                    enabled = bool(_safe_attr(info, "is_enabled", False))
                    name = str(_safe_attr(info, "name", "") or "").strip()
                    automation_id = str(_safe_attr(info, "automation_id", "") or "").strip()
                    if not name:
                        name = automation_id or f"{control_type} control"
                    rect = self._ui_rect(info)
                    read_only = bool(_safe_attr(info, "is_read_only", False))
                    current_state: dict[str, Any] = {}
                    if control_type in {"CheckBox", "ToggleButton"}:
                        try:
                            toggle_state = int(control.get_toggle_state())
                            current_state["toggle"] = {0: "off", 1: "on", 2: "indeterminate"}.get(
                                toggle_state, str(toggle_state)
                            )
                        except Exception:
                            pass
                    actions: list[str] = []
                    if enabled:
                        actions.extend(["focus", "press_key"])
                        if control_type in self._INTERACTIVE_TYPES:
                            actions.append("click")
                        if control_type == "Edit" and not read_only:
                            actions.extend(["type", "clear"])

                    discovered.append(
                        {
                            "native_key": native_key,
                            "window_id": window_id,
                            "window_title": state_window["title"],
                            "window_process_id": state_window["process_id"],
                            "application": state_window["application"],
                            "name": name,
                            "type": control_type,
                            "automation_id": automation_id or None,
                            "available_actions": actions,
                            "position": rect,
                            "state": current_state,
                            "enabled": enabled,
                            "source": "windows_uia",
                            "confidence": 0.9,
                        }
                    )
                    if len(discovered) >= maximum:
                        warnings.append(
                            f"Input discovery stopped at the configured limit of {maximum} controls."
                        )
                        break
                except Exception:
                    # A single malformed or inaccessible control should not hide its siblings.
                    continue

        return {
            "source": "windows_uia",
            "inputs": discovered,
            "warnings": warnings,
        }

    def execute_input(self, target: dict[str, Any], action: str, args: dict[str, Any]) -> dict[str, Any]:
        if os.name != "nt":
            raise ControllerError("Desktop actions require Windows.", "unsupported_platform", 501)
        try:
            from pywinauto import Desktop
        except ImportError:
            raise ControllerError(
                "pywinauto is required for UI Automation actions; install the Windows extras.",
                "dependency_missing",
                501,
            ) from None

        wrapper = self._resolve_input(Desktop, target)
        try:
            if action == "click":
                wrapper.click_input()
                return {"method": "windows_uia.click_input", "verified": None}
            if action == "focus":
                wrapper.set_focus()
                return {"method": "windows_uia.set_focus", "verified": None}
            if action in {"type", "clear"}:
                value = "" if action == "clear" else args.get("text")
                if not isinstance(value, str):
                    raise ControllerError("The type action requires a string 'text' value.", "invalid_action", 400)
                if len(value) > 10000:
                    raise ControllerError("Text is limited to 10,000 characters per action.", "payload_too_large", 413)
                wrapper.set_focus()
                wrapper.set_edit_text(value)
                verified: bool | None = None
                try:
                    current_value = wrapper.get_value()
                    verified = current_value == value
                except Exception:
                    pass
                return {
                    "method": "windows_uia.set_edit_text",
                    "verified": verified,
                    "verification": "value_matches" if verified else ("value_mismatch" if verified is False else "unavailable"),
                }
            if action == "press_key":
                keys = self._validate_keys(args.get("keys"))
                wrapper.set_focus()
                self._send_keys(keys)
                return {"method": "windows_keyboard.send_keys", "verified": None}
        except ControllerError:
            raise
        except Exception as exc:
            raise ControllerError(
                f"UI Automation action failed ({type(exc).__name__}).", "action_failed", 500
            ) from None
        raise ControllerError(f"Unsupported target action: {action}.", "unsupported_action", 400)

    def execute_window(self, window_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if os.name != "nt":
            raise ControllerError("Window actions require Windows.", "unsupported_platform", 501)
        window = self._find_window(window_id)
        hwnd = self._parse_window_id(window_id)
        self._focus_hwnd(hwnd, window_id)
        if action == "focus_window":
            return {"method": "win32.SetForegroundWindow", "window_id": window["window_id"], "verified": True}
        if action == "press_key":
            keys = self._validate_keys(args.get("keys"))
            self._send_keys(keys)
            return {"method": "windows_keyboard.send_keys", "window_id": window["window_id"], "verified": None}
        raise ControllerError(f"Unsupported window action: {action}.", "unsupported_action", 400)

    def execute_desktop(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if os.name != "nt":
            raise ControllerError("Desktop actions require Windows.", "unsupported_platform", 501)
        try:
            import pyautogui
        except ImportError:
            raise ControllerError(
                "pyautogui is required for coordinate actions; install the Windows extras.",
                "dependency_missing",
                501,
            ) from None

        if action == "click_at":
            x, y = self._coordinates(args)
            try:
                width, height = pyautogui.size()
                if x < 0 or y < 0 or x >= width or y >= height:
                    raise ControllerError("Coordinates are outside the primary screen.", "invalid_coordinates", 400)
                pyautogui.click(x=x, y=y)
            except ControllerError:
                raise
            except Exception as exc:
                raise ControllerError(
                    f"Coordinate click failed ({type(exc).__name__}).", "action_failed", 500
                ) from None
            return {"method": "pyautogui.click", "position": {"x": x, "y": y}, "verified": None}

        if action == "scroll_at":
            x, y = self._coordinates(args)
            delta = args.get("delta")
            if isinstance(delta, bool) or not isinstance(delta, int) or not -1000 <= delta <= 1000:
                raise ControllerError("'delta' must be an integer between -1000 and 1000.", "invalid_action", 400)
            try:
                width, height = pyautogui.size()
                if x < 0 or y < 0 or x >= width or y >= height:
                    raise ControllerError("Coordinates are outside the primary screen.", "invalid_coordinates", 400)
                pyautogui.scroll(delta, x=x, y=y)
            except ControllerError:
                raise
            except Exception as exc:
                raise ControllerError(
                    f"Coordinate scroll failed ({type(exc).__name__}).", "action_failed", 500
                ) from None
            return {"method": "pyautogui.scroll", "position": {"x": x, "y": y}, "delta": delta, "verified": None}

        raise ControllerError(f"Unsupported desktop action: {action}.", "unsupported_action", 400)

    def capture_screenshot(
        self,
        *,
        image_format: str = "png",
        quality: int = 85,
        scale: float = 1.0,
        window_id: str | None = None,
        region: dict[str, int] | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        if os.name != "nt":
            raise ControllerError("Screenshot capture is currently implemented for Windows only.", "unsupported_platform", 501)
        if window_id and region:
            raise ControllerError("Specify either 'window_id' or a crop region, not both.", "invalid_screenshot_request", 400)
        if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
            raise ControllerError("'quality' must be an integer from 1 to 100.", "invalid_screenshot_request", 400)
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0.1 <= float(scale) <= 1.0:
            raise ControllerError("'scale' must be between 0.1 and 1.0; upscaling is not supported.", "invalid_screenshot_request", 400)

        normalized_format = image_format.lower()
        if normalized_format in {"jpg", "jpeg"}:
            normalized_format = "jpeg"
            pil_format = "JPEG"
            mime = "image/jpeg"
        elif normalized_format == "png":
            pil_format = "PNG"
            mime = "image/png"
        elif normalized_format == "webp":
            pil_format = "WEBP"
            mime = "image/webp"
        else:
            raise ControllerError("Supported screenshot formats are png, jpeg, and webp.", "invalid_screenshot_request", 400)

        try:
            from PIL import Image, ImageGrab
        except ImportError:
            raise ControllerError(
                "Pillow is required for screenshot encoding; install the Windows extras.",
                "dependency_missing",
                501,
            ) from None

        requested_region = region
        if window_id:
            match = next(
                (item for item in self.get_state()["windows"] if item["window_id"] == window_id),
                None,
            )
            if match is None:
                raise ControllerError("The requested window is no longer available.", "stale_window", 409)
            requested_region = dict(match["bounds"])

        if requested_region is not None:
            requested_region = self._validate_region(requested_region)

        capture_started = time.perf_counter()
        capture_backend = "mss"
        try:
            import mss

            with mss.mss() as screen:
                virtual = screen.monitors[0]
                if requested_region is None:
                    actual = {
                        "left": int(virtual["left"]),
                        "top": int(virtual["top"]),
                        "width": int(virtual["width"]),
                        "height": int(virtual["height"]),
                    }
                else:
                    left = max(requested_region["x"], int(virtual["left"]))
                    top = max(requested_region["y"], int(virtual["top"]))
                    right = min(
                        requested_region["x"] + requested_region["width"],
                        int(virtual["left"]) + int(virtual["width"]),
                    )
                    bottom = min(
                        requested_region["y"] + requested_region["height"],
                        int(virtual["top"]) + int(virtual["height"]),
                    )
                    if right <= left or bottom <= top:
                        raise ControllerError("The requested crop is outside the visible desktop.", "invalid_region", 400)
                    actual = {"left": left, "top": top, "width": right - left, "height": bottom - top}
                shot = screen.grab(actual)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
        except ImportError:
            capture_backend = "Pillow.ImageGrab"
            try:
                if requested_region is None:
                    image = ImageGrab.grab(all_screens=True)
                    actual = {"left": 0, "top": 0, "width": image.width, "height": image.height}
                else:
                    left = requested_region["x"]
                    top = requested_region["y"]
                    right = left + requested_region["width"]
                    bottom = top + requested_region["height"]
                    image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
                    actual = {"left": left, "top": top, "width": image.width, "height": image.height}
                image = image.convert("RGB")
            except Exception as exc:
                raise ControllerError(
                    f"Screenshot capture failed ({type(exc).__name__}).", "capture_failed", 500
                ) from None
        except ControllerError:
            raise
        except Exception as exc:
            raise ControllerError(
                f"Screenshot capture failed ({type(exc).__name__}).", "capture_failed", 500
            ) from None

        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        encode_started = time.perf_counter()
        if float(scale) < 1.0:
            new_size = (
                max(1, round(image.width * float(scale))),
                max(1, round(image.height * float(scale))),
            )
            image = image.resize(new_size, Image.Resampling.BILINEAR)
        image = image.convert("RGB")

        buffer = io.BytesIO()
        try:
            if pil_format == "PNG":
                image.save(buffer, format=pil_format, compress_level=1, optimize=False)
            elif pil_format == "JPEG":
                image.save(buffer, format=pil_format, quality=quality, subsampling=1, optimize=False)
            else:
                image.save(buffer, format=pil_format, quality=quality, method=0)
        except Exception as exc:
            raise ControllerError(
                f"Screenshot encoding failed ({type(exc).__name__}).", "encoding_failed", 500
            ) from None
        encode_ms = (time.perf_counter() - encode_started) * 1000.0
        data = buffer.getvalue()
        metadata = {
            "format": normalized_format,
            "mime_type": mime,
            "width": image.width,
            "height": image.height,
            "bytes": len(data),
            "capture_ms": round(capture_ms, 3),
            "encode_ms": round(encode_ms, 3),
            "capture_backend": capture_backend,
            "region": {
                "x": actual["left"],
                "y": actual["top"],
                "width": actual["width"],
                "height": actual["height"],
            },
            "scale": float(scale),
            "captured_at": _now(),
        }
        return data, metadata

    def _list_processes(self) -> list[dict[str, Any]]:
        processes: list[dict[str, Any]] = []
        try:
            import psutil

            for process in psutil.process_iter(["pid", "name"]):
                try:
                    info = process.info
                    name = str(info.get("name") or "Unknown").strip()
                    pid = _as_int(info.get("pid"), -1)
                    if pid >= 0:
                        processes.append({"pid": pid, "name": name or "Unknown"})
                except Exception:
                    continue
        except ImportError:
            if os.name == "nt":
                processes = self._tasklist_processes()
            elif sys.platform.startswith("linux"):
                processes = self._procfs_processes()

        # Process enumeration may race with exits; de-duplicate and sort its result.
        deduplicated = {(item["pid"], item["name"]): item for item in processes}
        return sorted(deduplicated.values(), key=lambda item: (item["name"].casefold(), item["pid"]))

    @staticmethod
    def _procfs_processes() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        proc = Path("/proc")
        try:
            entries = list(proc.iterdir())
        except OSError:
            return result
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
                result.append({"pid": int(entry.name), "name": name or "Unknown"})
            except (OSError, ValueError):
                continue
        return result

    @staticmethod
    def _tasklist_processes() -> list[dict[str, Any]]:
        try:
            completed = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                check=False,
                capture_output=True,
                text=True,
                encoding="mbcs",
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        result: list[dict[str, Any]] = []
        for row in csv.reader(completed.stdout.splitlines()):
            if len(row) < 2:
                continue
            try:
                result.append({"pid": int(row[1].replace(",", "")), "name": row[0] or "Unknown"})
            except ValueError:
                continue
        return result

    def _windows(self, processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if os.name != "nt":
            return []
        try:
            import ctypes.wintypes as wintypes

            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowTextW.restype = ctypes.c_int
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.GetWindowRect.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            active_hwnd = self._hwnd_value(user32.GetForegroundWindow())
            process_names = {item["pid"]: item["name"] for item in processes}
            records: list[dict[str, Any]] = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def visit(hwnd: Any, _lparam: Any) -> bool:
                try:
                    handle = self._hwnd_value(hwnd)
                    if not handle or not user32.IsWindowVisible(hwnd):
                        return True
                    text_length = min(int(user32.GetWindowTextLengthW(hwnd)), 4096)
                    title_buffer = ctypes.create_unicode_buffer(text_length + 1)
                    user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
                    title = title_buffer.value.strip()
                    if not title:
                        return True

                    rect = wintypes.RECT()
                    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        return True
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    process_id = int(pid.value)
                    app_name = process_names.get(process_id) or self._query_process_name(process_id) or "Unknown"
                    records.append(
                        {
                            "window_id": self._window_id(handle, process_id),
                            "handle": handle,
                            "title": title,
                            "application": app_name,
                            "process_id": process_id,
                            "bounds": {
                                "x": int(rect.left),
                                "y": int(rect.top),
                                "width": max(0, int(rect.right - rect.left)),
                                "height": max(0, int(rect.bottom - rect.top)),
                            },
                            "is_active": handle == active_hwnd,
                            "is_minimized": bool(user32.IsIconic(hwnd)),
                            "visible": True,
                        }
                    )
                except Exception:
                    return True
                return True

            callback = callback_type(visit)
            enum_windows = user32.EnumWindows
            enum_windows.argtypes = [callback_type, wintypes.LPARAM]
            enum_windows.restype = wintypes.BOOL
            enum_windows(callback, 0)
            return records
        except Exception:
            # A window-enumeration failure should not take down process discovery or the API.
            return []

    @staticmethod
    def _query_process_name(pid: int) -> str | None:
        if os.name != "nt" or pid <= 0:
            return None
        try:
            import ctypes.wintypes as wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
            ]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not process:
                return None
            try:
                size = ctypes.c_ulong(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                    return None
                return os.path.basename(buffer.value)
            finally:
                kernel32.CloseHandle(process)
        except Exception:
            return None

    @staticmethod
    def _integration_status(process_names: set[str]) -> list[dict[str, Any]]:
        candidates = [
            ("Blender", {"blender.exe", "blender"}),
            ("Roblox Studio", {"robloxstudiobeta.exe", "robloxstudiobeta"}),
        ]
        integrations = []
        for name, executable_names in candidates:
            detected = bool(process_names.intersection(executable_names))
            integrations.append(
                {
                    "name": name,
                    "application_detected": detected,
                    "connected": False,
                    "status": "app_detected_no_connector" if detected else "not_detected",
                }
            )
        return integrations

    @staticmethod
    def _window_id(hwnd: int, process_id: int) -> str:
        # PID plus HWND keeps an ID stable as the window title changes, while invalidating it
        # when Windows reuses the handle for a different process.
        return f"win-{hwnd:x}-{process_id:x}"

    @staticmethod
    def _hwnd_value(hwnd: Any) -> int:
        try:
            return int(hwnd)
        except (TypeError, ValueError):
            return int(getattr(hwnd, "value", 0) or 0)

    @staticmethod
    def _parse_window_id(window_id: str) -> int:
        match = re.fullmatch(r"win-([0-9a-fA-F]+)-[0-9a-fA-F]+", str(window_id))
        if not match:
            raise ControllerError("Invalid window identifier.", "invalid_window_id", 400)
        return int(match.group(1), 16)

    @staticmethod
    def _ui_rect(info: Any) -> dict[str, int] | None:
        rect = _safe_attr(info, "rectangle")
        if rect is None:
            return None
        try:
            left, top = int(rect.left), int(rect.top)
            right, bottom = int(rect.right), int(rect.bottom)
            return {"x": left, "y": top, "width": max(0, right - left), "height": max(0, bottom - top)}
        except Exception:
            return None

    @classmethod
    def _ui_base_key(cls, hwnd: int, info: Any) -> str:
        runtime_id = _safe_attr(info, "runtime_id")
        if runtime_id:
            try:
                normalized_runtime_id = list(runtime_id)
            except TypeError:
                normalized_runtime_id = [str(runtime_id)]
            identity = ["runtime", hwnd, normalized_runtime_id]
        else:
            identity = [
                "fallback",
                hwnd,
                str(_safe_attr(info, "automation_id", "") or ""),
                str(_safe_attr(info, "control_type", "") or ""),
                str(_safe_attr(info, "name", "") or ""),
                cls._ui_rect(info),
            ]
        return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _validate_keys(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ControllerError("The press_key action requires a non-empty 'keys' string.", "invalid_action", 400)
        if len(value) > 64:
            raise ControllerError("Key sequences are limited to 64 characters.", "payload_too_large", 413)
        return value

    @staticmethod
    def _coordinates(args: dict[str, Any]) -> tuple[int, int]:
        x, y = args.get("x"), args.get("y")
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise ControllerError("'x' and 'y' must be integer screen coordinates.", "invalid_coordinates", 400)
        return x, y

    @staticmethod
    def _validate_region(region: dict[str, Any]) -> dict[str, int]:
        if not isinstance(region, dict):
            raise ControllerError("Crop region must be an object.", "invalid_region", 400)
        values = {key: region.get(key) for key in ("x", "y", "width", "height")}
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values.values()):
            raise ControllerError("Crop region requires integer x, y, width, and height values.", "invalid_region", 400)
        if values["width"] <= 0 or values["height"] <= 0 or values["width"] > 12000 or values["height"] > 12000:
            raise ControllerError("Crop width and height must be between 1 and 12,000 pixels.", "invalid_region", 400)
        return {key: int(value) for key, value in values.items()}

    def _find_window(self, window_id: str) -> dict[str, Any]:
        self._parse_window_id(window_id)
        match = next((item for item in self.get_state()["windows"] if item["window_id"] == window_id), None)
        if match is None:
            raise ControllerError("The requested window is no longer available.", "stale_window", 409)
        return match

    def _focus_hwnd(self, hwnd: int, expected_window_id: str | None = None) -> None:
        # Verify both handle and identity against a fresh window enumeration before focusing it.
        if not any(
            item.get("handle") == hwnd
            and (expected_window_id is None or item.get("window_id") == expected_window_id)
            for item in self.get_state()["windows"]
        ):
            raise ControllerError("The requested window is no longer available.", "stale_window", 409)
        try:
            import ctypes.wintypes as wintypes

            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.03)
            if self._hwnd_value(user32.GetForegroundWindow()) != hwnd:
                raise ControllerError(
                    "Windows did not grant foreground focus to that window.", "focus_denied", 409
                )
        except ControllerError:
            raise
        except Exception as exc:
            raise ControllerError(f"Could not focus the window ({type(exc).__name__}).", "focus_failed", 500) from None

    def _resolve_input(self, desktop_factory: Any, target: dict[str, Any]) -> Any:
        window_id = str(target.get("window_id") or "")
        hwnd = self._parse_window_id(window_id)
        current_window = self._find_window(window_id)
        if (
            current_window["process_id"] != target.get("window_process_id")
            or current_window["title"] != target.get("window_title")
        ):
            raise ControllerError("The input belongs to a changed window; refresh the input map.", "stale_target", 409)

        try:
            top_window = desktop_factory(backend="uia").window(handle=hwnd)
            controls = top_window.descendants()
        except Exception:
            raise ControllerError("The target window or its UI tree is no longer available.", "stale_target", 409) from None

        occurrences: dict[str, int] = {}
        for control in controls:
            try:
                info = control.element_info
                base_key = self._ui_base_key(hwnd, info)
                occurrence = occurrences.get(base_key, 0)
                occurrences[base_key] = occurrence + 1
                native_key = json.dumps([base_key, occurrence], ensure_ascii=False, separators=(",", ":"))
                if native_key == target.get("native_key"):
                    return control
            except Exception:
                continue
        raise ControllerError("The input no longer exists; refresh the input map.", "stale_target", 409)

    @staticmethod
    def _send_keys(keys: str) -> None:
        try:
            from pywinauto.keyboard import send_keys
        except ImportError:
            raise ControllerError("pywinauto is required to send keyboard shortcuts.", "dependency_missing", 501) from None
        try:
            send_keys(keys, with_spaces=True, pause=0.01)
        except Exception as exc:
            raise ControllerError(f"Keyboard input failed ({type(exc).__name__}).", "action_failed", 500) from None
