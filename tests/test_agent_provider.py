import base64
import io
import json
import unittest
import urllib.error
from email.message import Message

from arena_computer_controller.agent.openai_compatible import (
    ModelProviderError,
    OpenAICompatibleProvider,
)
from arena_computer_controller.agent.runtime import AgentRuntime
from arena_computer_controller.agent.tools import build_controller_tools
from arena_computer_controller.agent.types import ChatMessage, ImageAttachment, ToolSpec
from arena_computer_controller.integrations.mcp_bridge import ToolOutcome


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, amount=-1):
        return self.body if amount < 0 else self.body[:amount]


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def completion(message, finish_reason="stop"):
    return FakeResponse(json.dumps({"choices": [{"message": message, "finish_reason": finish_reason}]}).encode())


class OpenAICompatibleProviderTests(unittest.TestCase):
    def make_provider(self, opener, **overrides):
        values = {
            "base_url": "https://api.example.test/v1/",
            "api_key": "never-print-this-secret",
            "model": "vision-tool-model",
            "opener": opener,
        }
        values.update(overrides)
        return OpenAICompatibleProvider(**values)

    def test_tool_call_request_uses_openai_chat_completions_shape(self):
        opener = FakeOpener(completion({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "get_computer_state", "arguments": "{}"},
            }],
        }, "tool_calls"))
        provider = self.make_provider(opener)
        tool = ToolSpec(
            name="get_computer_state",
            description="Read state",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=lambda _args, _call_id: None,
        )

        turn = provider.complete([ChatMessage(role="user", content="Inspect the desktop")], [tool])

        request, timeout = opener.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer never-print-this-secret")
        self.assertEqual(timeout, 120.0)
        self.assertEqual(payload["model"], "vision-tool-model")
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertEqual(payload["tools"][0]["function"]["name"], "get_computer_state")
        self.assertEqual(turn.tool_calls[0].id, "call-1")
        self.assertEqual(turn.tool_calls[0].arguments, {})
        self.assertEqual(turn.finish_reason, "tool_calls")

    def test_runtime_and_provider_round_trip_state_then_real_screenshot_bytes(self):
        screenshot = b"\x89PNG\r\n\x1a\nend-to-end-image-bytes"

        class FakeBridge:
            def __init__(self):
                self.calls = []

            def get_computer_state(self):
                self.calls.append("get_computer_state")
                return ToolOutcome({"ok": True, "active_window": {"title": "Blender"}})

            def get_screenshot(self, format="jpeg", quality=90, scale=0.75, force=False, window_id=None):
                self.calls.append("get_screenshot")
                return ToolOutcome(
                    {"ok": True, "image_included": True, "mime_type": "image/png"},
                    image=screenshot,
                    image_mime_type="image/png",
                )

        opener = FakeOpener(
            completion({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "state-1",
                    "type": "function",
                    "function": {"name": "get_computer_state", "arguments": "{}"},
                }],
            }, "tool_calls"),
            completion({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "screen-1",
                    "type": "function",
                    "function": {"name": "get_screenshot", "arguments": "{\"format\":\"png\",\"scale\":0.5}"},
                }],
            }, "tool_calls"),
            completion({"role": "assistant", "content": "The screenshot shows Blender."}),
        )
        bridge = FakeBridge()
        runtime = AgentRuntime(
            self.make_provider(opener),
            build_controller_tools(bridge),
        )

        result = runtime.run("Describe my screen.")

        self.assertEqual(result.final_text, "The screenshot shows Blender.")
        self.assertEqual(bridge.calls, ["get_computer_state", "get_screenshot"])
        second_payload = json.loads(opener.requests[1][0].data.decode("utf-8"))
        assistant_call = next(
            message for message in second_payload["messages"]
            if message["role"] == "assistant" and message.get("tool_calls")
        )
        self.assertIsNone(assistant_call["content"])
        state_result = next(message for message in second_payload["messages"] if message["role"] == "tool")
        self.assertIn('"title":"Blender"', state_result["content"])
        third_payload = json.loads(opener.requests[2][0].data.decode("utf-8"))
        visual_message = next(
            message for message in third_payload["messages"]
            if message["role"] == "user" and isinstance(message["content"], list)
        )
        image_url = visual_message["content"][1]["image_url"]["url"]
        self.assertEqual(image_url, "data:image/png;base64," + base64.b64encode(screenshot).decode("ascii"))

    def test_screenshot_bytes_are_embedded_as_an_actual_multimodal_image(self):
        opener = FakeOpener(completion({"role": "assistant", "content": "I see a blue window."}))
        provider = self.make_provider(opener)
        image = b"\x89PNG\r\n\x1a\nsmall-test-image"
        messages = [
            ChatMessage(role="tool", name="get_screenshot", tool_call_id="call-shot", content='{"ok":true}'),
            ChatMessage(
                role="user",
                content="Inspect the image returned by get_screenshot.",
                images=(ImageAttachment("image/png", image),),
            ),
        ]

        provider.complete(messages, [])

        payload = json.loads(opener.requests[0][0].data.decode("utf-8"))
        image_part = payload["messages"][1]["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        self.assertEqual(
            image_part["image_url"]["url"],
            "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
        )
        self.assertNotIn("file://", image_part["image_url"]["url"])
        self.assertNotIn("screenshot_path", json.dumps(payload))

    def test_rejects_non_loopback_plain_http_api_urls(self):
        for url in (
            "http://api.example.test/v1",
            "https://user:password@api.example.test/v1",
            "https://api.example.test/v1?token=secret",
            "file:///tmp/model",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.make_provider(FakeOpener(), base_url=url)
        self.assertEqual(
            self.make_provider(FakeOpener(), base_url="http://127.0.0.1:3000/v1").base_url,
            "http://127.0.0.1:3000/v1",
        )

    def test_http_error_is_safe_and_does_not_echo_credentials_or_body(self):
        headers = Message()
        error = urllib.error.HTTPError(
            "https://api.example.test/v1/chat/completions",
            401,
            "unauthorized",
            headers,
            io.BytesIO(b"never-print-this-secret"),
        )
        provider = self.make_provider(FakeOpener(error))
        with self.assertRaises(ModelProviderError) as caught:
            provider.complete([ChatMessage(role="user", content="hello")], [])
        self.assertIn("authentication", str(caught.exception))
        self.assertNotIn("never-print-this-secret", str(caught.exception))
        self.assertNotIn("unauthorized", str(caught.exception))
        self.assertEqual(caught.exception.status, 401)

    def test_bad_tool_arguments_and_invalid_response_are_rejected(self):
        malformed_call = completion({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {"name": "test", "arguments": "not-json"}}],
        }, "tool_calls")
        provider = self.make_provider(FakeOpener(malformed_call))
        with self.assertRaisesRegex(ModelProviderError, "invalid JSON tool arguments"):
            provider.complete([ChatMessage(role="user", content="hello")], [])

        provider = self.make_provider(FakeOpener(FakeResponse(b"not-json")))
        with self.assertRaisesRegex(ModelProviderError, "invalid JSON"):
            provider.complete([ChatMessage(role="user", content="hello")], [])

        non_finite_call = completion({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {"name": "test", "arguments": "{\\\"value\\\":NaN}"}}],
        }, "tool_calls")
        provider = self.make_provider(FakeOpener(non_finite_call))
        with self.assertRaisesRegex(ModelProviderError, "invalid JSON tool arguments"):
            provider.complete([ChatMessage(role="user", content="hello")], [])

        repeated_id = completion({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "same-id", "function": {"name": "first", "arguments": "{}"}},
                {"id": "same-id", "function": {"name": "second", "arguments": "{}"}},
            ],
        }, "tool_calls")
        provider = self.make_provider(FakeOpener(repeated_id))
        with self.assertRaisesRegex(ModelProviderError, "duplicate tool-call IDs"):
            provider.complete([ChatMessage(role="user", content="hello")], [])

    def test_request_and_response_limits_are_enforced(self):
        provider = self.make_provider(FakeOpener(completion({"role": "assistant", "content": "ok"})), max_request_bytes=10)
        with self.assertRaisesRegex(ModelProviderError, "request exceeds"):
            provider.complete([ChatMessage(role="user", content="a long prompt")], [])

        provider = self.make_provider(FakeOpener(FakeResponse(b"x" * 20)), max_response_bytes=10)
        with self.assertRaisesRegex(ModelProviderError, "response exceeded"):
            provider.complete([ChatMessage(role="user", content="hello")], [])

    def test_empty_api_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "API key is required"):
            self.make_provider(FakeOpener(), api_key=" ")


if __name__ == "__main__":
    unittest.main()
