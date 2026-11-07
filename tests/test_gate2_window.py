from __future__ import annotations

import unittest

from arena_computer_controller.gate2.protocol import MARKER_COLORS
from arena_computer_controller.gate2.window import TEST_WINDOW_TITLE, MarkerTestWindow


class FakeCanvas:
    def __init__(self):
        self.configured = []

    def itemconfigure(self, marker_id, **kwargs):
        self.configured.append((marker_id, kwargs))


class FakeRoot:
    def __init__(self):
        self.attributes_calls = []
        self.title_value = None
        self.geometry_value = None
        self.resizable_value = None
        self.overrideredirect_value = None
        self.config_value = None
        self.protocols = {}
        self.after_calls = []
        self.destroyed = False

    def title(self, value):
        self.title_value = value

    def geometry(self, value):
        self.geometry_value = value

    def resizable(self, *value):
        self.resizable_value = value

    def overrideredirect(self, value):
        self.overrideredirect_value = value

    def configure(self, **kwargs):
        self.config_value = kwargs

    def attributes(self, *args):
        self.attributes_calls.append(args)

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def after(self, _delay, callback):
        self.after_calls.append(_delay)
        callback()

    def update_idletasks(self):
        pass

    def winfo_id(self):
        return 0x1234

    def destroy(self):
        self.destroyed = True


class FakeTkModule:
    class Label:
        def __init__(self, *_args, **_kwargs): pass
        def pack(self, **_kwargs): pass

    class Canvas(FakeCanvas):
        def __init__(self, *_args, **_kwargs): super().__init__()
        def pack(self, **_kwargs): pass
        def create_rectangle(self, *_args, **_kwargs): return 9

    class Frame:
        def __init__(self, *_args, **_kwargs): pass
        def pack(self, **_kwargs): pass

    class Button:
        def __init__(self, *_args, **_kwargs): pass
        def grid(self, **_kwargs): pass
        def pack(self, **_kwargs): pass

    class StringVar:
        def __init__(self, value=""):
            self.value = value
        def set(self, value): self.value = value


class Gate2WindowTests(unittest.TestCase):
    def test_window_is_opaque_fixed_topmost_and_hides_initial_color(self):
        import sys
        from unittest.mock import patch

        root = FakeRoot()
        with patch.dict(sys.modules, {"tkinter": FakeTkModule()}):
            window = MarkerTestWindow(root, color_picker=lambda colors: "red")
        self.assertEqual(root.title_value, TEST_WINDOW_TITLE)
        self.assertEqual(root.geometry_value, "500x440")
        self.assertEqual(window.native_handle, 0x1234)
        self.assertTrue(root.overrideredirect_value)
        self.assertEqual(root.resizable_value, (False, False))
        self.assertIn(("-topmost", True), root.attributes_calls)
        self.assertEqual(window._marker_color, "red")
        self.assertNotIn("red", root.title_value.casefold())
        self.assertEqual(window._status.value, "Test window ready")
        self.assertNotIn(window._marker_color, window._status.value)
        self.assertEqual(set(MARKER_COLORS), {"red", "green", "blue", "yellow", "purple"})

    def test_only_fixed_palette_can_change_marker_and_close_is_clean(self):
        import sys
        from unittest.mock import patch

        root = FakeRoot()
        closed = []
        with patch.dict(sys.modules, {"tkinter": FakeTkModule()}):
            window = MarkerTestWindow(root, on_close=lambda: closed.append(True), color_picker=lambda colors: "green")
        window.set_marker_color("blue")
        self.assertEqual(window._marker_color, "blue")
        with self.assertRaises(ValueError):
            window.set_marker_color("magenta")
        window.close()
        self.assertTrue(root.destroyed)
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
