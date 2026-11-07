from __future__ import annotations

import contextlib
import io
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from arena_computer_controller.gate2.capture import CaptureSafetyError, CapturedPng
from arena_computer_controller.gate2.local_test import (
    LocalTestError,
    _save_unique_png,
    run_local_test,
)


def make_png(width: int = 500, height: int = 440, rgb: tuple[int, int, int] = (50, 100, 200)) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(bytes([0]) + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    signature = bytes.fromhex("89504e470d0a1a0a")
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class FakeRoot:
    def __init__(self):
        self.callbacks = []
        self.after_delays = []
        self.destroyed = False
        self.mainloop_calls = 0

    def after(self, delay, callback):
        self.after_delays.append(delay)
        self.callbacks.append((delay, callback))

    def mainloop(self):
        self.mainloop_calls += 1
        while self.callbacks and not self.destroyed:
            _delay, callback = self.callbacks.pop(0)
            callback()

    def destroy(self):
        self.destroyed = True


class FakeWindow:
    native_handle = 0x1234

    def __init__(self, root, events=None):
        self.root = root
        self.statuses = []
        self.events = events if events is not None else []

    def raise_for_capture(self):
        self.events.append("raised")

    def set_status(self, text):
        self.statuses.append(text)


class FakeCapture:
    def __init__(self, result=None, error=None, events=None):
        self.result = result or CapturedPng(make_png(), 500, 440)
        self.error = error
        self.calls = 0
        self.events = events if events is not None else []

    def capture_png(self):
        self.calls += 1
        self.events.append("captured")
        if self.error:
            raise self.error
        return self.result


class Gate2LocalTestTests(unittest.TestCase):
    def test_saves_verified_png_without_overwriting_existing_files(self):
        png = make_png()
        with tempfile.TemporaryDirectory() as directory:
            first = _save_unique_png(png, Path(directory))
            second = _save_unique_png(png, Path(directory))
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), png)
            self.assertEqual(second.read_bytes(), png)
            self.assertTrue(first.name.startswith("arena-gate2-local-"))
            self.assertEqual(first.suffix, ".png")

    def test_local_test_captures_once_reports_path_and_dimensions_and_leaves_window_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = FakeRoot()
            events = []
            window = FakeWindow(root, events)
            capture = FakeCapture(events=events)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = run_local_test(
                    output_directory=Path(directory),
                    root_factory=lambda: root,
                    window_factory=lambda value: window,
                    capture_factory=lambda *, target_handle: self._capture_for(target_handle, capture),
                    dpi_setup=lambda: None,
                    is_windows=True,
                    capture_delay_ms=10,
                )
            files = list(Path(directory).glob("*.png"))
            self.assertEqual(result, 0)
            self.assertEqual(capture.calls, 1)
            self.assertEqual(root.after_delays, [10, 100])
            self.assertEqual(root.mainloop_calls, 1)
            self.assertEqual(events, ["raised", "captured"])
            self.assertFalse(root.destroyed, "the window stays open for local marker-button testing")
            self.assertEqual(window.statuses, ["Local PNG saved; no cloud connection"])
            self.assertEqual(len(files), 1)
            self.assertIn(str(files[0]), stdout.getvalue())
            self.assertIn("Dimensions: 500 x 440 pixels", stdout.getvalue())
            self.assertEqual(files[0].read_bytes(), capture.result.data)

    @staticmethod
    def _capture_for(target_handle, capture):
        if target_handle != FakeWindow.native_handle:
            raise AssertionError("only the test-window HWND may be captured")
        return capture

    def test_capture_safety_failure_saves_nothing_and_closes_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = FakeRoot()
            window = FakeWindow(root)
            capture = FakeCapture(error=CaptureSafetyError("The test window is obscured."))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = run_local_test(
                    output_directory=Path(directory),
                    root_factory=lambda: root,
                    window_factory=lambda value: window,
                    capture_factory=lambda **_kwargs: capture,
                    dpi_setup=lambda: None,
                    is_windows=True,
                )
            self.assertEqual(result, 2)
            self.assertEqual(capture.calls, 1)
            self.assertTrue(root.destroyed)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertIn("obscured", stderr.getvalue())

    def test_invalid_png_saves_nothing_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = FakeRoot()
            capture = FakeCapture(result=CapturedPng(b"not a png", 500, 440))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = run_local_test(
                    output_directory=Path(directory),
                    root_factory=lambda: root,
                    window_factory=FakeWindow,
                    capture_factory=lambda **_kwargs: capture,
                    dpi_setup=lambda: None,
                    is_windows=True,
                )
            self.assertEqual(result, 2)
            self.assertTrue(root.destroyed)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertIn("valid PNG", stderr.getvalue())

    def test_non_windows_host_fails_before_creating_a_window(self):
        called = []
        with self.assertRaises(LocalTestError):
            run_local_test(
                root_factory=lambda: called.append("root"),
                dpi_setup=lambda: called.append("dpi"),
                is_windows=False,
            )
        self.assertEqual(called, [])

    def test_cli_local_test_bypasses_config_credentials_and_supabase(self):
        from arena_computer_controller.gate2 import cli

        with (
            patch.object(cli, "run_local_test", return_value=0) as local_test,
            patch.object(cli, "_config_path", side_effect=AssertionError("config path was touched")),
            patch.object(cli, "load_config", side_effect=AssertionError("config was loaded")),
            patch.object(cli, "load_pairing_token", side_effect=AssertionError("token was loaded")),
            patch.object(cli, "SupabaseRpcClient", side_effect=AssertionError("RPC client was created")),
            patch.object(cli, "Gate2Companion", side_effect=AssertionError("polling companion was started")),
        ):
            result = cli.main(["--config", "must-not-be-read.json", "local-test"])
        self.assertEqual(result, 0)
        local_test.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
