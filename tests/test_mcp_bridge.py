import io
import json
import unittest
import urllib.error
from email.message import Message
from urllib.parse import parse_qs, urlsplit

from arena_computer_controller.integrations.mcp_bridge import (
    BridgeError,
    ControllerHTTPClient,
    ControllerMCPBridge,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"test-png-payload"
JPEG = b"\xff\xd8\xff" + b"test-jpeg-payload"


class FakeResponse:
    def __init__(self, body, headers=None, status=200):
        self.body = body
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, amount=-1):
        if amount is None or amount < 0:
            return self.body
        return self.body[:amount]


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


class ControllerHTTPClientTests(unittest.TestCase):
    def test_controller_destination_must_be_plain_http_loopback(self):
        for url in (
            "https://127.0.0.1:8765",
            "http://example.com:8765",
            "http://192.168.1.20:8765",
            "http://user:password@127.0.0.1:8765",
            "http://127.0.0.1:8765/some/path",
        ):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                ControllerHTTPClient(url)
        self.assertEqual(ControllerHTTPClient("http://[::1]:8765").base_url, "http://[::1]:8765")

    def test_json_request_adds_bearer_auth_and_decodes_object(self):
        opener = FakeOpener(FakeResponse(b'{"ok":true,"version":"test"}', {"Content-Type": "application/json"}))
        client = ControllerHTTPClient(token="local-secret", opener=opener)
        result = client.get_json("/v1/state")
        request, timeout = opener.requests[0]
        self.assertEqual(result, {"ok": True, "version": "test"})
        self.assertEqual(request.get_header("Authorization"), "Bearer local-secret")
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/v1/state")
        self.assertEqual(timeout, 20.0)

    def test_invalid_server_json_and_non_object_json_are_rejected(self):
        for body in (b"not-json", b"[]"):
            client = ControllerHTTPClient(opener=FakeOpener(FakeResponse(body)))
            with self.subTest(body=body), self.assertRaises(BridgeError):
                client.get_json("/v1/state")

    def test_http_error_is_structured_without_losing_controller_response(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/state",
            503,
            "unavailable",
            headers,
            io.BytesIO(b'{"error":{"code":"adapter_unavailable","message":"Blender adapter unavailable"}}'),
        )
        outcome = ControllerMCPBridge(ControllerHTTPClient(opener=FakeOpener(error))).get_computer_state()
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.data["http_status"], 503)
        self.assertEqual(outcome.data["error"]["code"], "adapter_unavailable")
        self.assertEqual(outcome.data["controller_response"]["error"]["message"], "Blender adapter unavailable")

    def test_screenshot_validation_occurs_before_http_request(self):
        opener = FakeOpener()
        bridge = ControllerMCPBridge(ControllerHTTPClient(opener=opener))
        outcome = bridge.get_screenshot(format="gif")
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.data["error"]["code"], "invalid_screenshot_format")
        self.assertEqual(opener.requests, [])

    def test_blender_probe_uses_existing_probe_route(self):
        opener = FakeOpener(FakeResponse(b'{"healthy":true,"job_id":"probe-1"}'))
        outcome = ControllerMCPBridge(ControllerHTTPClient(opener=opener)).blender_probe(timeout=3.5)
        request, timeout = opener.requests[0]
        self.assertFalse(outcome.is_error)
        self.assertEqual(outcome.data["job_id"], "probe-1")
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/v1/integrations/blender/probe")
        self.assertEqual(json.loads(request.data), {"timeout": 3.5})
        self.assertEqual(timeout, 13.5)

    def test_blender_execute_forwards_only_operation_fields_and_request_id(self):
        opener = FakeOpener(FakeResponse(b'{"ok":true,"job_id":"job-1","status":"completed"}'))
        bridge = ControllerMCPBridge(ControllerHTTPClient(opener=opener))
        outcome = bridge.blender_execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "Subject"},
            timeout=12,
            request_id="retry-stable-id",
        )
        request, timeout = opener.requests[0]
        self.assertFalse(outcome.is_error)
        self.assertEqual(outcome.data["request_id"], "retry-stable-id")
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/v1/integrations/blender/commands")
        self.assertEqual(json.loads(request.data), {
            "operation": "create_primitive",
            "parameters": {"primitive_type": "cube", "object_name": "Subject"},
            "timeout": 12.0,
            "request_id": "retry-stable-id",
        })
        self.assertEqual(timeout, 22.0)

    def test_timeout_and_screenshot_scale_overflow_return_structured_errors(self):
        opener = FakeOpener()
        bridge = ControllerMCPBridge(ControllerHTTPClient(opener=opener))
        command = bridge.blender_execute("create_test_object", timeout=10**1000, request_id="safe-id")
        screenshot = bridge.get_screenshot(scale=10**1000)
        self.assertEqual(command.data["error"]["code"], "invalid_timeout")
        self.assertEqual(screenshot.data["error"]["code"], "invalid_screenshot_scale")
        self.assertEqual(opener.requests, [])

    def test_blender_execute_rejects_non_object_parameters_without_http(self):
        opener = FakeOpener()
        outcome = ControllerMCPBridge(ControllerHTTPClient(opener=opener)).blender_execute("create_primitive", ["cube"])
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.data["error"]["code"], "invalid_parameters")
        self.assertEqual(opener.requests, [])

    def test_oversized_controller_request_is_rejected_before_http(self):
        opener = FakeOpener()
        bridge = ControllerMCPBridge(ControllerHTTPClient(opener=opener))
        outcome = bridge.blender_execute(
            "create_test_object",
            {"large": "x" * 1_000_000},
            request_id="large-request",
        )
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.data["error"]["code"], "request_too_large")
        self.assertEqual(opener.requests, [])

    def test_desktop_image_is_attached_once_and_deduplicated_by_bytes(self):
        opener = FakeOpener(
            FakeResponse(JPEG, {"Content-Type": "image/jpeg", "X-Image-Width": "100", "X-Image-Height": "50"}),
            FakeResponse(JPEG, {"Content-Type": "image/jpeg", "X-Image-Width": "100", "X-Image-Height": "50"}),
            FakeResponse(JPEG, {"Content-Type": "image/jpeg"}),
        )
        bridge = ControllerMCPBridge(ControllerHTTPClient(opener=opener))
        first = bridge.get_screenshot()
        second = bridge.get_screenshot()
        forced = bridge.get_screenshot(force=True)
        self.assertEqual(first.image, JPEG)
        self.assertEqual(first.image_mime_type, "image/jpeg")
        self.assertEqual(first.data["width"], 100)
        self.assertFalse(first.data["identical_to_previous"])
        self.assertIsNone(second.image)
        self.assertTrue(second.data["identical_to_previous"])
        self.assertFalse(second.data["image_included"])
        self.assertEqual(forced.image, JPEG)
        self.assertEqual(len(opener.requests), 3)
        self.assertIn("format=jpeg", opener.requests[0][0].full_url)

    def test_window_screenshot_url_encodes_window_id_and_rejects_controls(self):
        opener = FakeOpener(FakeResponse(JPEG, {"Content-Type": "image/jpeg"}))
        bridge = ControllerMCPBridge(ControllerHTTPClient(opener=opener))
        outcome = bridge.get_screenshot(window_id="win-1&other=yes")
        request = opener.requests[0][0]
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["window_id"], ["win-1&other=yes"])
        self.assertEqual(outcome.data["window_id"], "win-1&other=yes")
        invalid = bridge.get_screenshot(window_id="win-1\nheader")
        self.assertTrue(invalid.is_error)
        self.assertEqual(invalid.data["error"]["code"], "invalid_window_id")
        self.assertEqual(len(opener.requests), 1)

    def test_blender_screenshot_validates_job_id_and_png_payload(self):
        opener = FakeOpener(
            FakeResponse(PNG, {"Content-Type": "image/png", "X-Blender-Capture-Type": "camera_render"}),
            FakeResponse(PNG, {"Content-Type": "image/png"}),
        )
        bridge = ControllerMCPBridge(ControllerHTTPClient(opener=opener))
        invalid = bridge.blender_get_screenshot("../escape")
        self.assertTrue(invalid.is_error)
        self.assertEqual(opener.requests, [])
        result = bridge.blender_get_screenshot("job-123")
        request = opener.requests[0][0]
        self.assertEqual(result.image, PNG)
        self.assertEqual(result.data["capture_type"], "camera_render")
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/v1/integrations/blender/jobs/job-123/screenshot")
        duplicate = bridge.blender_get_screenshot("job-123")
        self.assertTrue(duplicate.data["identical_to_previous"])
        self.assertIsNone(duplicate.image)

    def test_declared_image_type_must_match_signature(self):
        opener = FakeOpener(FakeResponse(PNG, {"Content-Type": "image/jpeg"}))
        outcome = ControllerMCPBridge(ControllerHTTPClient(opener=opener)).get_screenshot()
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.data["error"]["code"], "invalid_image_payload")

    def test_image_response_is_bounded(self):
        with self.assertRaises(BridgeError):
            ControllerHTTPClient(max_image_bytes=100)
        oversized_png = b"\x89PNG\r\n\x1a\n" + b"x" * 1024
        opener = FakeOpener(FakeResponse(oversized_png, {"Content-Type": "image/png"}))
        client = ControllerHTTPClient(max_image_bytes=1024, opener=opener)
        outcome = ControllerMCPBridge(client).get_screenshot(format="png")
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.data["error"]["code"], "response_too_large")


if __name__ == "__main__":
    unittest.main()
