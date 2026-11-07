from __future__ import annotations

import os
import struct
import unittest
import zlib

from arena_computer_controller.gate2.capture import (
    CaptureSafetyError,
    WindowsWindowCapture,
    _validate_topmost_and_unobscured,
    rects_intersect,
)
from arena_computer_controller.gate2.protocol import MAX_SCREENSHOT_BYTES
from arena_computer_controller.gate2.window import TEST_WINDOW_HEIGHT, TEST_WINDOW_TITLE, TEST_WINDOW_WIDTH


def make_png(width: int, height: int, rgb: tuple[int, int, int] = (50, 100, 200)) -> bytes:
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


class FakeBackend:
    def __init__(self, *, region=None, data=None, width=None, height=None):
        self.region = region
        self.data = data or make_png(TEST_WINDOW_WIDTH, TEST_WINDOW_HEIGHT)
        self.width = width if width is not None else TEST_WINDOW_WIDTH
        self.height = height if height is not None else TEST_WINDOW_HEIGHT
        self.calls = []

    def get_state(self):
        raise AssertionError("Gate 2 must not enumerate unrelated windows or processes.")

    def capture_screenshot(self, *, image_format, window_id=None, region=None):
        self.calls.append((image_format, window_id, region))
        return self.data, {
            "width": self.width,
            "height": self.height,
            "region": self.region or {"x": 40, "y": 50, "width": TEST_WINDOW_WIDTH, "height": TEST_WINDOW_HEIGHT},
        }


class Gate2CaptureTests(unittest.TestCase):
    @staticmethod
    def bounds():
        return {"x": 40, "y": 50, "width": TEST_WINDOW_WIDTH, "height": TEST_WINDOW_HEIGHT}

    def make_capture(self, backend, *, safe=True, bounds=None):
        fixed_bounds = bounds or self.bounds()
        return WindowsWindowCapture(
            backend,
            target_handle=0x1234,
            platform_check=lambda: True,
            window_info_provider=lambda handle, pid, title: (handle, fixed_bounds),
            occlusion_check=lambda hwnd, pid, rect: safe,
        )

    def test_capture_uses_only_the_exact_verified_window_crop(self):
        backend = FakeBackend()
        result = self.make_capture(backend).capture_png()
        expected_bounds = self.bounds()
        self.assertEqual((result.width, result.height), (TEST_WINDOW_WIDTH, TEST_WINDOW_HEIGHT))
        self.assertEqual(backend.calls, [("png", None, expected_bounds)])
        self.assertLessEqual(len(result.data), MAX_SCREENSHOT_BYTES)

    def test_refuses_occlusion_without_calling_screenshot_backend(self):
        backend = FakeBackend()
        capture = self.make_capture(backend, safe=False)
        with self.assertRaises(CaptureSafetyError):
            capture.capture_png()
        self.assertEqual(backend.calls, [])

    def test_specific_win32_occlusion_reason_is_preserved_and_capture_is_not_attempted(self):
        backend = FakeBackend()

        def unsafe(*_args):
            raise CaptureSafetyError("A visible window overlaps the test window.")

        capture = WindowsWindowCapture(
            backend,
            target_handle=0x1234,
            platform_check=lambda: True,
            window_info_provider=lambda handle, pid, title: (handle, self.bounds()),
            occlusion_check=unsafe,
        )
        with self.assertRaisesRegex(CaptureSafetyError, "visible window overlaps"):
            capture.capture_png()
        self.assertEqual(backend.calls, [])

    def test_refuses_wrong_target_dimensions_without_capture(self):
        backend = FakeBackend()
        bounds = self.bounds() | {"width": 499}
        with self.assertRaises(CaptureSafetyError):
            self.make_capture(backend, bounds=bounds).capture_png()
        self.assertEqual(backend.calls, [])

    def test_refuses_clipped_or_mismatched_capture(self):
        backend = FakeBackend(region={"x": 40, "y": 50, "width": 499, "height": TEST_WINDOW_HEIGHT})
        with self.assertRaises(CaptureSafetyError):
            self.make_capture(backend).capture_png()

    def test_refuses_metadata_dimensions_that_disagree_with_bounds(self):
        backend = FakeBackend(width=499)
        with self.assertRaises(CaptureSafetyError):
            self.make_capture(backend).capture_png()

    def test_post_capture_occlusion_discards_the_image(self):
        backend = FakeBackend()
        safety_checks = iter((True, False))
        capture = WindowsWindowCapture(
            backend,
            target_handle=0x1234,
            platform_check=lambda: True,
            window_info_provider=lambda handle, pid, title: (handle, self.bounds()),
            occlusion_check=lambda *_args: next(safety_checks),
        )
        with self.assertRaises(CaptureSafetyError):
            capture.capture_png()
        self.assertEqual(len(backend.calls), 1)

    def test_platform_or_missing_handle_failure_never_falls_back_to_full_desktop(self):
        backend = FakeBackend()
        capture = WindowsWindowCapture(backend, target_handle=0x1234, platform_check=lambda: False)
        with self.assertRaises(CaptureSafetyError):
            capture.capture_png()
        self.assertEqual(backend.calls, [])
        no_handle = WindowsWindowCapture(backend, platform_check=lambda: True)
        with self.assertRaises(CaptureSafetyError):
            no_handle.capture_png()
        self.assertEqual(backend.calls, [])

    def test_target_window_title_is_scoped_to_the_test_app(self):
        backend = FakeBackend()
        inspected = []
        capture = WindowsWindowCapture(
            backend,
            target_handle=0x1234,
            platform_check=lambda: True,
            window_info_provider=lambda handle, pid, title: (inspected.append((handle, pid, title)) or handle, self.bounds()),
            occlusion_check=lambda *_args: True,
        )
        capture.capture_png()
        self.assertEqual(inspected, [(0x1234, os.getpid(), TEST_WINDOW_TITLE)] * 2)

    def test_topmost_style_is_required_and_failure_is_distinguished(self):
        with self.assertRaisesRegex(CaptureSafetyError, "did not confirm.*topmost"):
            _validate_topmost_and_unobscured(0, self.bounds(), [])
        _validate_topmost_and_unobscured(0x00000008, self.bounds(), [])

    def test_visible_overlap_still_fails_even_when_test_window_is_topmost(self):
        overlapping = {"x": 45, "y": 55, "width": 80, "height": 60}
        with self.assertRaisesRegex(CaptureSafetyError, "visible window overlaps"):
            _validate_topmost_and_unobscured(0x00000008, self.bounds(), [overlapping])

    def test_rect_intersection(self):
        a = {"x": 0, "y": 0, "width": 10, "height": 10}
        self.assertTrue(rects_intersect(a, {"x": 9, "y": 9, "width": 5, "height": 5}))
        self.assertFalse(rects_intersect(a, {"x": 10, "y": 0, "width": 5, "height": 5}))


if __name__ == "__main__":
    unittest.main()
