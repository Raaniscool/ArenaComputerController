"""Local-only launch and window-scoped PNG diagnostic for Gate 2."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .capture import (
    CaptureSafetyError,
    WindowsWindowCapture,
    enable_capture_dpi_awareness,
)
from .protocol import ProtocolError, screenshot_fields
from .window import MarkerTestWindow


class LocalTestError(RuntimeError):
    """A local Gate 2 window or screenshot diagnostic failed safely."""


def _create_tk_root() -> Any:
    import tkinter as tk

    return tk.Tk()


def _save_unique_png(data: bytes, output_directory: Path | None = None) -> Path:
    """Save one PNG without replacing any existing file."""
    directory = Path(output_directory) if output_directory is not None else Path(tempfile.gettempdir())
    if not directory.is_dir():
        raise LocalTestError("The local screenshot directory is unavailable.")

    for _ in range(5):
        path = directory / f"arena-gate2-local-{uuid4().hex}.png"
        created = False
        try:
            with path.open("xb") as stream:
                created = True
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            return path
        except FileExistsError:
            continue
        except OSError:
            if created:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise LocalTestError("The local PNG could not be saved without overwriting a file.") from None
    raise LocalTestError("A unique local PNG filename could not be reserved.")


def _destroy_root(root: Any) -> None:
    try:
        root.destroy()
    except Exception:
        pass


def run_local_test(
    *,
    output_directory: Path | None = None,
    root_factory: Callable[[], Any] | None = None,
    window_factory: Callable[[Any], Any] | None = None,
    capture_factory: Callable[..., Any] | None = None,
    dpi_setup: Callable[[], None] | None = None,
    is_windows: bool | None = None,
    capture_delay_ms: int = 750,
) -> int:
    """Open the existing test window, save one verified crop, and stay local.

    This function intentionally has no configuration, credential, RPC, or
    companion-client dependency. The injectable factories are used by the
    tests; the CLI supplies no overrides.
    """
    windows_host = os.name == "nt" if is_windows is None else is_windows
    if not windows_host:
        raise LocalTestError("The Gate 2 local test requires an interactive Windows session.")
    if isinstance(capture_delay_ms, bool) or not isinstance(capture_delay_ms, int) or not 0 <= capture_delay_ms <= 10_000:
        raise LocalTestError("The local capture delay is invalid.")

    try:
        (dpi_setup or enable_capture_dpi_awareness)()
    except Exception:
        raise LocalTestError("Per-monitor DPI awareness could not be enabled; safe capture is unavailable.") from None

    try:
        root = (root_factory or _create_tk_root)()
    except Exception:
        raise LocalTestError("Tkinter could not open the local Gate 2 test window.") from None

    try:
        window = (window_factory or MarkerTestWindow)(root)
        capture = (capture_factory or WindowsWindowCapture)(target_handle=window.native_handle)
    except Exception:
        _destroy_root(root)
        raise LocalTestError("The local test-window capture target could not be initialized safely.") from None

    saved: list[tuple[Path, int, int]] = []
    failures: list[str] = []

    def fail(message: str) -> None:
        failures.append(message)
        print(f"Gate 2 local test failed: {message}", file=sys.stderr)
        _destroy_root(root)

    def capture_once() -> None:
        try:
            image = capture.capture_png()
            try:
                screenshot_fields(image.data, image.width, image.height)
            except ProtocolError:
                raise LocalTestError("The scoped capture was not a valid PNG with matching dimensions.") from None
            path = _save_unique_png(image.data, output_directory)
            saved.append((path, image.width, image.height))
            try:
                window.set_status("Local PNG saved; no cloud connection")
            except Exception:
                pass
            print(f"Local PNG: {path}")
            print(f"Dimensions: {image.width} x {image.height} pixels")
        except CaptureSafetyError as exc:
            fail(str(exc))
        except LocalTestError as exc:
            fail(str(exc))
        except Exception:
            fail("The local screenshot capture failed safely.")

    try:
        root.after(capture_delay_ms, capture_once)
        root.mainloop()
    except KeyboardInterrupt:
        _destroy_root(root)
        raise
    except Exception:
        _destroy_root(root)
        raise LocalTestError("The local Gate 2 window stopped unexpectedly.") from None

    if failures:
        return 2
    if not saved:
        print("Gate 2 local test failed: the window closed before capture completed.", file=sys.stderr)
        return 2
    return 0
