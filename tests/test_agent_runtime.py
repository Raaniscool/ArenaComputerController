import unittest

from arena_workshop.protocol import SUPPORTED_OPERATIONS

from arena_computer_controller.agent import (
    AgentRuntime,
    AssistantTurn,
    ChatMessage,
    ImageAttachment,
    ToolCall,
    build_controller_tools,
)
from arena_computer_controller.integrations.mcp_bridge import ToolOutcome


PNG = b"\x89PNG\r\n\x1a\ntest-image-bytes"


class FakeBridge:
    def __init__(self):
        self.calls = []

    def get_computer_state(self):
        self.calls.append(("get_computer_state",))
        return ToolOutcome({"ok": True, "active_window": {"title": "Blender"}, "windows": []})

    def get_active_window(self):
        self.calls.append(("get_active_window",))
        return ToolOutcome({"ok": True, "active_window": {"title": "Blender"}})

    def get_screenshot(self, format="jpeg", quality=90, scale=0.75, force=False, window_id=None):
        self.calls.append(("get_screenshot", format, quality, scale, force, window_id))
        return ToolOutcome(
            {"ok": True, "image_included": True, "mime_type": "image/png", "sha256": "abc"},
            image=PNG,
            image_mime_type="image/png",
        )

    def blender_probe(self, timeout=5.0):
        self.calls.append(("blender_probe", timeout))
        return ToolOutcome({"ok": True, "healthy": True})

    def blender_execute(self, operation, parameters=None, timeout=60.0, request_id=None):
        self.calls.append(("blender_execute", operation, parameters, timeout, request_id))
        return ToolOutcome({"ok": True, "operation": operation, "request_id": request_id})

    def blender_get_screenshot(self, job_id, force=False):
        self.calls.append(("blender_get_screenshot", job_id, force))
        return ToolOutcome({"ok": True, "job_id": job_id, "image_included": False})


class FakeProvider:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((tuple(messages), tuple(tools)))
        return self.turns.pop(0)


class AgentRuntimeTests(unittest.TestCase):
    def test_runs_state_then_screenshot_and_passes_image_bytes_back_to_model(self):
        bridge = FakeBridge()
        provider = FakeProvider([
            AssistantTurn(tool_calls=(ToolCall("call-state", "get_computer_state", {}),)),
            AssistantTurn(tool_calls=(ToolCall("call-shot", "get_screenshot", {"format": "png", "scale": 0.5}),)),
            AssistantTurn(content="The screenshot shows the Blender window."),
        ])
        events = []
        runtime = AgentRuntime(
            provider,
            build_controller_tools(bridge),
            progress_callback=events.append,
        )

        result = runtime.run("Describe what is on my computer screen.")

        self.assertEqual(result.final_text, "The screenshot shows the Blender window.")
        self.assertEqual(result.tool_call_count, 2)
        self.assertEqual(bridge.calls[0], ("get_computer_state",))
        self.assertEqual(bridge.calls[1], ("get_screenshot", "png", 90, 0.5, False, None))
        last_messages = provider.requests[2][0]
        tool_result = next(message for message in last_messages if message.role == "tool" and message.tool_call_id == "call-shot")
        visual_input = next(message for message in last_messages if message.images)
        self.assertIn('"sha256":"abc"', tool_result.content)
        self.assertEqual(visual_input.role, "user")
        self.assertEqual(visual_input.images, (ImageAttachment("image/png", PNG),))
        self.assertEqual(events[0].kind, "task_started")
        self.assertTrue(any(event.kind == "tool_completed" and event.data.get("image_included") for event in events))

    def test_multiple_tool_replies_precede_image_messages(self):
        bridge = FakeBridge()
        provider = FakeProvider([
            AssistantTurn(tool_calls=(
                ToolCall("call-shot", "get_screenshot", {"format": "png"}),
                ToolCall("call-state", "get_computer_state", {}),
            )),
            AssistantTurn(content="Both tool results were received."),
        ])
        runtime = AgentRuntime(provider, build_controller_tools(bridge))

        result = runtime.run("Inspect the desktop")

        self.assertEqual(result.final_text, "Both tool results were received.")
        followup = provider.requests[1][0]
        roles_after_call = [message.role for message in followup[3:]]
        self.assertEqual(roles_after_call, ["tool", "tool", "user"])
        self.assertEqual(len(followup[-1].images), 1)

    def test_mutating_blender_tool_is_denied_without_explicit_approval(self):
        bridge = FakeBridge()
        provider = FakeProvider([
            AssistantTurn(tool_calls=(ToolCall(
                "call-create",
                "blender_execute",
                {"operation": "create_primitive", "parameters": {"primitive_type": "cube", "object_name": "AgentCube"}},
            ),)),
            AssistantTurn(content="I did not make the change because approval was not granted."),
        ])
        runtime = AgentRuntime(provider, build_controller_tools(bridge))

        result = runtime.run("Create a cube in Blender.")

        self.assertEqual(result.final_text, "I did not make the change because approval was not granted.")
        self.assertFalse(any(call[0] == "blender_execute" for call in bridge.calls))
        tool_message = next(message for message in provider.requests[1][0] if message.role == "tool")
        self.assertIn("approval_denied", tool_message.content)

    def test_approved_blender_tool_uses_stable_request_id_derived_from_call_id(self):
        bridge = FakeBridge()
        provider = FakeProvider([
            AssistantTurn(tool_calls=(ToolCall("call-create-01", "blender_execute", {"operation": "hello_test"}),)),
            AssistantTurn(content="The approved probe completed."),
        ])
        approvals = []

        def approve(tool, call):
            approvals.append((tool.name, call.id))
            return True

        runtime = AgentRuntime(provider, build_controller_tools(bridge), approval_callback=approve)
        result = runtime.run("Run the harmless Blender hello test.")

        execution = next(call for call in bridge.calls if call[0] == "blender_execute")
        self.assertEqual(approvals, [("blender_execute", "call-create-01")])
        self.assertEqual(execution[1], "hello_test")
        self.assertTrue(execution[4].startswith("agent-"))
        self.assertEqual(len(execution[4]), 46)
        self.assertEqual(result.stop_reason, "completed")

    def test_tool_errors_are_returned_to_the_model_without_crashing_runtime(self):
        provider = FakeProvider([
            AssistantTurn(tool_calls=(ToolCall("call-1", "not_a_tool", {}),)),
            AssistantTurn(content="That tool is not available."),
        ])
        runtime = AgentRuntime(provider, build_controller_tools(FakeBridge()))
        result = runtime.run("Do something")
        self.assertEqual(result.final_text, "That tool is not available.")
        tool_message = next(message for message in provider.requests[1][0] if message.role == "tool")
        self.assertIn("unknown_tool", tool_message.content)

    def test_tool_call_budget_stops_before_executing_unbounded_requests(self):
        bridge = FakeBridge()
        provider = FakeProvider([
            AssistantTurn(tool_calls=(ToolCall("call-1", "get_computer_state", {}),)),
            AssistantTurn(tool_calls=(ToolCall("call-2", "get_computer_state", {}),)),
            AssistantTurn(content="stopped"),
        ])
        runtime = AgentRuntime(provider, build_controller_tools(bridge), max_tool_calls=1)
        result = runtime.run("Read state repeatedly")
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(result.stop_reason, "max_tool_calls")
        self.assertEqual([call[0] for call in bridge.calls], ["get_computer_state"])

    def test_empty_task_and_duplicate_tool_names_are_rejected(self):
        provider = FakeProvider([])
        runtime = AgentRuntime(provider, build_controller_tools(FakeBridge()))
        with self.assertRaises(ValueError):
            runtime.run("  ")
        tools = build_controller_tools(FakeBridge())
        with self.assertRaisesRegex(ValueError, "unique"):
            AgentRuntime(provider, [tools[0], tools[0]])

    def test_controller_tool_surface_keeps_existing_six_names_and_blender_allowlist(self):
        tools = build_controller_tools(FakeBridge())
        names = {tool.name for tool in tools}
        self.assertEqual(names, {
            "get_computer_state",
            "get_active_window",
            "get_screenshot",
            "blender_probe",
            "blender_execute",
            "blender_get_screenshot",
        })
        blender_tool = next(tool for tool in tools if tool.name == "blender_execute")
        operation_values = blender_tool.parameters["properties"]["operation"]["enum"]
        self.assertEqual(tuple(operation_values), SUPPORTED_OPERATIONS)


if __name__ == "__main__":
    unittest.main()
