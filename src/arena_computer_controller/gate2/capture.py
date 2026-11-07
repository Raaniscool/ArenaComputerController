"""Fail-closed capture of the Gate 2 app's visible window bounds only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from ..backend import SystemDesktopBackend
from .protocol import MAX_IMAGE_HEIGHT, MAX_IMAGE_WIDTH, MAX_SCREENSHOT_BYTES
from .window import TEST_WINDOW_HEIGHT, TEST_WINDOW_TITLE, TEST_WINDOW_WIDTH


_WS_EX_TOPMOST = 0x00000008


class CaptureSafetyError(RuntimeError):
    """The target window cannot be proven safe to capture."""


def enable_capture_dpi_awareness() -> None:
    """Set Windows per-monitor-v2 awareness before creating the Tk window."""
    if os.name != "nt":
        raise CaptureSafetyError("Windows DPI awareness is unavailable on this host.")
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_process_context = user32.SetProcessDpiAwarenessContext
        get_thread_context = user32.GetThreadDpiAwarenessContext
        contexts_equal = user32.AreDpiAwarenessContextsEqual
        set_process_context.argtypes = [ctypes.c_void_p]
        set_process_context.restype = wintypes.BOOL
        get_thread_context.argtypes = []
        get_thread_context.restype = ctypes.c_void_p
        contexts_equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        contexts_equal.restype = wintypes.BOOL
        per_monitor_v2 = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if set_process_context(per_monitor_v2):
            return
        current = get_thread_context()
        if current and contexts_equal(current, per_monitor_v2):
            return  # An application manifest or host already enabled the required mode.
    except Exception:
        pass
    raise CaptureSafetyError("Per-monitor DPI awareness could not be enabled for safe window capture.")


@dataclass(frozen=True)
class CapturedPng:
    data: bytes
    width: int
    height: int


class WindowsWindowCapture:
    """Capture only the known Tk test window through an exact desktop crop.

    The general backend's ``window_id`` path calls ``get_state()`` and
    enumerates window/process metadata. Gate 2 instead starts from the Tk root
    HWND, validates only that HWND, and reuses the backend's exact-region crop
    path. By default overlapping windows are rejected; the explicit local-test
    mode may allow them while retaining target identity and topmost checks. The
    pixels remain a visible-desktop capture, not an isolated/offscreen capture.
    """

    def __init__(
        self,
        backend: Any | None = None,
        *,
        target_handle: int | None = None,
        title: str = TEST_WINDOW_TITLE,
        platform_check: Callable[[], bool] | None = None,
        window_info_provider: Callable[[int, int, str], tuple[int, dict[str, int]]] | None = None,
        occlusion_check: Callable[[int, int, dict[str, int]], bool] | None = None,
        reject_overlapping_windows: bool = True,
    ) -> None:
        if not isinstance(reject_overlapping_windows, bool):
            raise ValueError("reject_overlapping_windows must be a boolean.")
        self.backend = backend or SystemDesktopBackend()
        self.target_handle = target_handle
        self.title = title
        self._platform_check = platform_check or (lambda: os.name == "nt")
        self._window_info_provider = window_info_provider or _win32_window_info
        self._occlusion_check = occlusion_check
        self._reject_overlapping_windows = reject_overlapping_windows

    def _is_target_window_safe(self, hwnd: int, process_id: int, bounds: dict[str, int]) -> bool:
        if self._occlusion_check is not None:
            return self._occlusion_check(hwnd, process_id, bounds)
        return _win32_window_is_safe(
            hwnd,
            process_id,
            bounds,
            reject_overlapping_windows=self._reject_overlapping_windows,
        )

    def capture_png(self) -> CapturedPng:
        if not self._platform_check():
            raise CaptureSafetyError("Windows screenshot capture is unavailable on this host.")
        if isinstance(self.target_handle, bool) or not isinstance(self.target_handle, int) or self.target_handle <= 0:
            raise CaptureSafetyError("The Gate 2 test-window handle is unavailable.")
        process_id = os.getpid()
        try:
            handle, bounds = self._window_info_provider(self.target_handle, process_id, self.title)
            if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
                raise CaptureSafetyError("The Gate 2 test window could not be identified safely.")
            normalized_bounds = _validate_bounds(bounds)
            if (normalized_bounds["width"], normalized_bounds["height"]) != (TEST_WINDOW_WIDTH, TEST_WINDOW_HEIGHT):
                raise CaptureSafetyError("The Gate 2 test window does not have its fixed capture dimensions.")
            if not self._is_target_window_safe(handle, process_id, normalized_bounds):
                raise CaptureSafetyError("The test window is not topmost or could not be verified safely.")

            # Always pass an exact crop region. There is no full-desktop fallback.
            data, metadata = self.backend.capture_screenshot(image_format="png", region=normalized_bounds)
            post_handle, post_bounds = self._window_info_provider(self.target_handle, process_id, self.title)
            if (
                post_handle != handle
                or _validate_bounds(post_bounds) != normalized_bounds
                or not self._is_target_window_safe(handle, process_id, normalized_bounds)
            ):
                raise CaptureSafetyError("The test window changed or could not be verified during capture.")
            region = metadata.get("region") if isinstance(metadata, dict) else None
            if not isinstance(region, dict) or any(
                region.get(key) != normalized_bounds[key] for key in ("x", "y", "width", "height")
            ):
                raise CaptureSafetyError("The capture was clipped or did not match the test window bounds.")
            width = metadata.get("width")
            height = metadata.get("height")
            if (
                not isinstance(data, bytes)
                or len(data) > MAX_SCREENSHOT_BYTES
                or isinstance(width, bool)
                or not isinstance(width, int)
                or isinstance(height, bool)
                or not isinstance(height, int)
                or width != normalized_bounds["width"]
                or height != normalized_bounds["height"]
                or width > MAX_IMAGE_WIDTH
                or height > MAX_IMAGE_HEIGHT
            ):
                raise CaptureSafetyError("The test-window screenshot is invalid or too large.")
            return CapturedPng(data=data, width=width, height=height)
        except CaptureSafetyError:
            raise
        except Exception:
            # Do not include window titles, paths, backend exceptions, or image state in logs.
            raise CaptureSafetyError("Safe test-window capture failed.") from None


def _validate_bounds(bounds: dict[str, Any]) -> dict[str, int]:
    if not isinstance(bounds, dict):
        raise CaptureSafetyError("Test window bounds are invalid.")
    result: dict[str, int] = {}
    for key in ("x", "y", "width", "height"):
        value = bounds.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise CaptureSafetyError("Test window bounds are invalid.")
        result[key] = value
    if result["width"] < 1 or result["height"] < 1:
        raise CaptureSafetyError("Test window bounds are invalid.")
    if result["width"] > MAX_IMAGE_WIDTH or result["height"] > MAX_IMAGE_HEIGHT:
        raise CaptureSafetyError("Test window exceeds the configured capture size limit.")
    return result


def rects_intersect(a: dict[str, int], b: dict[str, int]) -> bool:
    return (
        a["x"] < b["x"] + b["width"]
        and a["x"] + a["width"] > b["x"]
        and a["y"] < b["y"] + b["height"]
        and a["y"] + a["height"] > b["y"]
    )


def _validate_topmost_and_overlaps(
    extended_style: int,
    bounds: dict[str, int],
    visible_windows_above: list[dict[str, int]],
    *,
    reject_overlapping_windows: bool = True,
) -> None:
    """Always require topmost; optionally reject intersecting visible windows."""
    if isinstance(extended_style, bool) or not isinstance(extended_style, int) or not extended_style & _WS_EX_TOPMOST:
        raise CaptureSafetyError("Windows did not confirm that the test window is topmost.")
    if not reject_overlapping_windows:
        return
    for other_bounds in visible_windows_above:
        if other_bounds["width"] > 0 and other_bounds["height"] > 0 and rects_intersect(bounds, other_bounds):
            raise CaptureSafetyError("A visible window overlaps the test window; close or move it before retrying.")


def _validate_window_handle(handle: int) -> None:
    if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
        raise CaptureSafetyError("The Gate 2 test window could not be identified safely.")


def _validate_window_visibility(is_visible: bool, is_minimized: bool) -> None:
    if not is_visible or is_minimized:
        raise CaptureSafetyError("The Gate 2 test window is hidden or minimized.")


def _validate_window_process(actual_pid: int, expected_pid: int) -> None:
    if actual_pid != expected_pid:
        raise CaptureSafetyError("The Gate 2 test window belongs to another process.")


def _validate_window_title(actual_title: str, expected_title: str) -> None:
    if not isinstance(actual_title, str) or actual_title != expected_title:
        raise CaptureSafetyError("The Gate 2 test window title could not be verified.")


def _win32_window_info(target_handle: int, expected_pid: int, expected_title: str) -> tuple[int, dict[str, int]]:
    """Read only the target window identity/bounds; never enumerate desktop titles."""
    if os.name != "nt":
        raise CaptureSafetyError("Windows window inspection is unavailable on this host.")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    requested = wintypes.HWND(target_handle)
    top_level = user32.GetAncestor(requested, 2)  # GA_ROOT; Tk may return a child HWND.
    handle = _as_hwnd_value(top_level)
    _validate_window_handle(handle)
    _validate_window_visibility(bool(user32.IsWindowVisible(top_level)), bool(user32.IsIconic(top_level)))

    actual_pid = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(top_level, ctypes.byref(actual_pid)):
        raise CaptureSafetyError("The Gate 2 test window process could not be verified.")
    _validate_window_process(int(actual_pid.value), expected_pid)

    title_length = int(user32.GetWindowTextLengthW(top_level))
    if title_length < 1 or title_length > 4096:
        raise CaptureSafetyError("The Gate 2 test window title could not be verified.")
    title_buffer = ctypes.create_unicode_buffer(title_length + 1)
    copied = int(user32.GetWindowTextW(top_level, title_buffer, len(title_buffer)))
    if copied <= 0:
        raise CaptureSafetyError("The Gate 2 test window title could not be verified.")
    _validate_window_title(title_buffer.value, expected_title)

    rect = wintypes.RECT()
    if not user32.GetWindowRect(top_level, ctypes.byref(rect)):
        raise CaptureSafetyError("The Gate 2 test window bounds could not be read.")
    bounds = {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }
    return handle, bounds


def _win32_window_is_safe(
    hwnd: int,
    expected_pid: int,
    bounds: dict[str, int],
    *,
    reject_overlapping_windows: bool = True,
) -> bool:
    """Require topmost; optionally reject overlaps from visible windows above."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_window_long.restype = ctypes.c_ssize_t
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    target = wintypes.HWND(hwnd)
    if not user32.IsWindowVisible(target) or user32.IsIconic(target):
        raise CaptureSafetyError("The test window is not visible or is minimized.")
    target_pid = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(target, ctypes.byref(target_pid)) or int(target_pid.value) != expected_pid:
        raise CaptureSafetyError("The verified test-window handle changed ownership.")
    exstyle = int(get_window_long(target, -20))  # GWL_EXSTYLE

    rect = wintypes.RECT()
    if not user32.GetWindowRect(target, ctypes.byref(rect)):
        raise CaptureSafetyError("The test-window bounds could not be confirmed.")
    current_bounds = {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }
    if current_bounds != bounds:
        raise CaptureSafetyError("The test window moved while its Z-order was being checked.")

    if not reject_overlapping_windows:
        # Local test mode deliberately permits visible windows to cover this crop.
        # Keep the target's own topmost style check, but do not enumerate peers.
        _validate_topmost_and_overlaps(
            exstyle,
            bounds,
            [],
            reject_overlapping_windows=False,
        )
        return True

    current = user32.GetWindow(target, 3)  # GW_HWNDPREV: windows above this one in Z order.
    visited: set[int] = set()
    visible_windows_above: list[dict[str, int]] = []
    for _ in range(4096):
        current_value = _as_hwnd_value(current)
        if not current_value:
            break
        if current_value in visited:
            raise CaptureSafetyError("The test-window Z-order could not be verified safely.")
        visited.add(current_value)
        candidate = wintypes.HWND(current_value)
        current = user32.GetWindow(candidate, 3)
        if not user32.IsWindowVisible(candidate) or user32.IsIconic(candidate):
            continue
        candidate_rect = wintypes.RECT()
        if not user32.GetWindowRect(candidate, ctypes.byref(candidate_rect)):
            # If a visible window cannot be inspected, fail closed.
            raise CaptureSafetyError("A visible window above the test window could not be checked safely.")
        visible_windows_above.append(
            {
                "x": int(candidate_rect.left),
                "y": int(candidate_rect.top),
                "width": int(candidate_rect.right - candidate_rect.left),
                "height": int(candidate_rect.bottom - candidate_rect.top),
            }
        )
    else:
        raise CaptureSafetyError("The test-window Z-order exceeded the verification limit.")

    _validate_topmost_and_overlaps(
        exstyle,
        bounds,
        visible_windows_above,
        reject_overlapping_windows=reject_overlapping_windows,
    )
    return True


def _as_hwnd_value(value: Any) -> int:
    try:
        return int(getattr(value, "value", value) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
