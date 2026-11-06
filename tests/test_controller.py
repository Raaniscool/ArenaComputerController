from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arena_computer_controller.controller import ComputerController
from arena_computer_controller.errors import ControllerError
from tests.fakes import FakeBackend


class ComputerControllerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.controller = ComputerController(self.backend)

    def test_input_ids_are_revision_bound(self):
        first = self.controller.get_inputs()
        first_id = first["inputs"][0]["id"]
        second = self.controller.get_inputs()
        second_id = second["inputs"][0]["id"]

        self.assertNotEqual(first["revision"], second["revision"])
        self.assertNotEqual(first_id, second_id)
        with self.assertRaises(ControllerError) as error:
            self.controller.perform_action(
                {"action": "click", "target": first_id, "expected_revision": first["revision"]}
            )
        self.assertEqual(error.exception.code, "stale_input_map")

    def test_action_returns_state_delta_and_redacts_typed_content_from_history(self):
        input_map = self.controller.get_inputs()
        item = input_map["inputs"][0]
        result = self.controller.perform_action(
            {
                "action": "type",
                "target": item["id"],
                "expected_revision": input_map["revision"],
                "text": "must-not-be-stored-in-history",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["state_changes"][0]["type"], "window_opened")
        self.assertTrue(result["input_refresh_recommended"])
        history = self.controller.history()
        self.assertEqual(len(history), 1)
        self.assertNotIn("must-not-be-stored-in-history", repr(history))

    def test_available_actions_are_enforced(self):
        input_map = self.controller.get_inputs()
        item = input_map["inputs"][0]
        with self.assertRaises(ControllerError) as error:
            self.controller.perform_action(
                {"action": "click", "target": item["id"], "expected_revision": input_map["revision"]}
            )
        self.assertEqual(error.exception.code, "unsupported_action")

    def test_target_actions_require_current_revision(self):
        input_map = self.controller.get_inputs()
        item = input_map["inputs"][0]
        with self.assertRaises(ControllerError) as error:
            self.controller.perform_action({"action": "focus", "target": item["id"]})
        self.assertEqual(error.exception.code, "revision_required")

    def test_window_level_key_action_does_not_require_input_map(self):
        result = self.controller.perform_action(
            {"action": "press_key", "window_id": "win-1", "keys": "{ENTER}"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.backend.executed[-1][1], "press_key")

    def test_history_limit_is_bounded(self):
        controller = ComputerController(self.backend, history_limit=2)
        for _ in range(3):
            with self.assertRaises(ControllerError):
                controller.perform_action({"action": "not-an-action"})
        self.assertEqual(len(controller.history(200)), 2)


if __name__ == "__main__":
    unittest.main()
