import unittest

from arena_computer_controller.agent.cli import _check_controller_ready, build_parser


class FakeControllerClient:
    def __init__(self, health):
        self.health = health
        self.calls = []

    def get_json(self, path, *, timeout):
        self.calls.append((path, timeout))
        return self.health


class AgentCLITests(unittest.TestCase):
    def test_controller_preflight_accepts_healthy_loopback_response(self):
        client = FakeControllerClient({"ok": True, "version": "0.1.0"})
        health = _check_controller_ready(client)
        self.assertEqual(health["version"], "0.1.0")
        self.assertEqual(client.calls, [("/health", 5.0)])

    def test_controller_preflight_fails_before_agent_loop_if_not_healthy(self):
        client = FakeControllerClient({"ok": False})
        with self.assertRaisesRegex(ValueError, "health check"):
            _check_controller_ready(client)
        self.assertEqual(client.calls, [("/health", 5.0)])

    def test_cli_parser_reads_validated_execution_budgets(self):
        args = build_parser().parse_args([
            "--max-turns", "3",
            "--max-tool-calls", "0",
            "Describe the screen",
        ])
        self.assertEqual(args.max_turns, 3)
        self.assertEqual(args.max_tool_calls, 0)
        self.assertEqual(args.task, ["Describe the screen"])


if __name__ == "__main__":
    unittest.main()
