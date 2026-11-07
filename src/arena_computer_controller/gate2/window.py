"""Opaque, isolated Tk test window with one harmless color-change control."""

from __future__ import annotations

import secrets
import threading
from typing import Callable

from .protocol import MARKER_COLORS


TEST_WINDOW_TITLE = "Arena Gate 2 Test Window"
TEST_WINDOW_WIDTH = 500
TEST_WINDOW_HEIGHT = 440


class MarkerTestWindow:
    """One visible marker and a fixed palette; no general desktop-control API."""

    def __init__(
        self,
        root,
        *,
        on_close: Callable[[], None] | None = None,
        color_picker: Callable[[tuple[str, ...]], str] | None = None,
    ) -> None:
        import tkinter as tk

        self.root = root
        self._ui_thread = threading.current_thread()
        self._on_close = on_close
        self._closing = False
        self._color_picker = color_picker or secrets.choice
        self._marker_color = self._color_picker(tuple(MARKER_COLORS))
        if not isinstance(self._marker_color, str) or self._marker_color not in MARKER_COLORS:
            raise ValueError("Initial marker color must be from the fixed palette.")

        root.title(TEST_WINDOW_TITLE)
        root.overrideredirect(True)
        root.geometry(f"{TEST_WINDOW_WIDTH}x{TEST_WINDOW_HEIGHT}")
        root.resizable(False, False)
        root.configure(background="#F2F4F7")
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)

        tk.Label(
            root,
            text="Gate 2 visual test",
            font=("Segoe UI", 14, "bold"),
            background="#F2F4F7",
            foreground="#243244",
        ).pack(pady=(14, 4))
        tk.Label(
            root,
            text="The large square is the target. Use a color button to change it.",
            font=("Segoe UI", 9),
            background="#F2F4F7",
            foreground="#35465D",
        ).pack(pady=(0, 8))

        self._canvas = tk.Canvas(
            root,
            width=210,
            height=170,
            background="#FFFFFF",
            highlightthickness=1,
            highlightbackground="#AEB8C5",
            borderwidth=0,
        )
        self._canvas.pack(pady=4)
        self._marker_id = self._canvas.create_rectangle(
            45, 25, 165, 145,
            fill=MARKER_COLORS[self._marker_color],
            outline="#1F2937",
            width=2,
        )

        controls = tk.Frame(root, background="#F2F4F7")
        controls.pack(pady=(10, 4))
        for index, color in enumerate(MARKER_COLORS):
            tk.Button(
                controls,
                text=f"Set {color}",
                width=9,
                command=lambda selected=color: self.set_marker_color(selected),
            ).grid(row=index // 3, column=index % 3, padx=4, pady=4)

        self._status = tk.StringVar(value="Test window ready")
        tk.Label(
            root,
            textvariable=self._status,
            font=("Segoe UI", 9),
            background="#F2F4F7",
            foreground="#35465D",
            wraplength=450,
        ).pack(pady=(4, 6))
        tk.Button(root, text="Close test window", command=self.close).pack(pady=(0, 8))

    @property
    def native_handle(self) -> int:
        """Return only this Tk root's native handle for scoped capture checks."""
        try:
            self.root.update_idletasks()
            handle = self.root.winfo_id()
            if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
                raise ValueError
            return handle
        except Exception:
            raise RuntimeError("The test-window handle could not be read safely.") from None

    def set_marker_color(self, color: str, *, timeout_seconds: float = 5.0) -> None:
        """Set only the marker fill, running the Tk update on its owning thread."""
        if not isinstance(color, str) or color not in MARKER_COLORS:
            raise ValueError("Color is not in the fixed Gate 2 palette.")
        if self._closing:
            raise RuntimeError("The test window is closing.")

        def apply() -> None:
            self._canvas.itemconfigure(self._marker_id, fill=MARKER_COLORS[color])
            self._marker_color = color
            self.root.update_idletasks()

        if threading.current_thread() is self._ui_thread:
            apply()
            return

        completed = threading.Event()
        error: list[BaseException] = []

        def apply_and_signal() -> None:
            try:
                if self._closing:
                    raise RuntimeError("The test window is closing.")
                apply()
            except BaseException as exc:
                error.append(exc)
            finally:
                completed.set()

        try:
            self.root.after(0, apply_and_signal)
        except Exception:
            raise RuntimeError("The test window is not accepting actions.") from None
        if not completed.wait(timeout_seconds):
            raise TimeoutError("The test window did not apply the approved color in time.")
        if error:
            raise RuntimeError("The test window could not apply the approved color.") from None

    def set_status(self, text: str) -> None:
        """Show a color-free connection/status message in the test window."""
        if not isinstance(text, str):
            return
        safe_text = text[:160]

        def update() -> None:
            if not self._closing:
                self._status.set(safe_text)

        try:
            if threading.current_thread() is self._ui_thread:
                update()
            else:
                self.root.after(0, update)
        except Exception:
            pass

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass
