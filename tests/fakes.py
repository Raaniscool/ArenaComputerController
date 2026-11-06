from __future__ import annotations

import copy


class FakeBackend:
    def __init__(self):
        self.state = {
            "timestamp": "2026-11-06T00:00:00Z",
            "platform": {"system": "TestOS", "release": "1"},
            "platform_supported": True,
            "active_window": {"window_id": "win-1", "title": "Editor", "application": "Editor"},
            "windows": [
                {
                    "window_id": "win-1",
                    "title": "Editor",
                    "application": "Editor",
                    "process_id": 17,
                    "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
                    "is_active": True,
                }
            ],
            "running_applications": [{"name": "Editor", "process_count": 1, "has_window": True}],
            "running_processes": [{"pid": 17, "name": "editor.exe"}],
            "integrations": [],
        }
        self.inputs = [
            {
                "native_key": "ui-element-one",
                "window_id": "win-1",
                "window_title": "Editor",
                "window_process_id": 17,
                "application": "Editor",
                "name": "Search field",
                "type": "Edit",
                "automation_id": "search",
                "available_actions": ["focus", "press_key", "type", "clear"],
                "position": {"x": 10, "y": 10, "width": 200, "height": 30},
                "enabled": True,
                "source": "test",
                "confidence": 1.0,
            }
        ]
        self.executed = []

    def capabilities(self):
        return {"platform_supported": True, "screenshots": True}

    def get_state(self):
        return copy.deepcopy(self.state)

    def discover_inputs(self):
        return {"source": "test", "inputs": copy.deepcopy(self.inputs), "warnings": []}

    def execute_input(self, target, action, args):
        self.executed.append((target["id"], action, dict(args)))
        if action in {"click", "type"}:
            new_window = {
                "window_id": "win-2",
                "title": "Result",
                "application": "Editor",
                "process_id": 17,
                "bounds": {"x": 20, "y": 20, "width": 300, "height": 200},
                "is_active": False,
            }
            self.state["windows"].append(new_window)
        return {"method": "fake", "verified": True}

    def execute_window(self, window_id, action, args):
        self.executed.append((window_id, action, dict(args)))
        return {"method": "fake-window", "verified": True}

    def execute_desktop(self, action, args):
        self.executed.append((None, action, dict(args)))
        return {"method": "fake-desktop", "verified": None}

    def capture_screenshot(self, **options):
        return b"image", {
            "mime_type": "image/png",
            "width": 1,
            "height": 1,
            "bytes": 5,
            "capture_ms": 1.0,
            "encode_ms": 2.0,
            "capture_backend": "fake",
        }
