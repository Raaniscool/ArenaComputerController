from __future__ import annotations

import json
import threading
import unittest
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arena_computer_controller.controller import ComputerController
from arena_computer_controller.server import create_server
from tests.fakes import FakeBackend


class HTTPServerTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server(ComputerController(FakeBackend()), "127.0.0.1", 0, token="test-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, path, *, method="GET", payload=None, auth=True):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = "Bearer test-secret"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        return urlopen(request, timeout=3)

    def test_health_is_public_but_state_requires_auth(self):
        with self._request("/health", auth=False) as response:
            health = json.loads(response.read())
        self.assertTrue(health["ok"])

        with self.assertRaises(HTTPError) as error:
            self._request("/v1/state", auth=False)
        self.assertEqual(error.exception.code, 401)

    def test_input_map_and_action_round_trip(self):
        with self._request("/v1/inputs") as response:
            input_map = json.loads(response.read())
        item = input_map["inputs"][0]
        with self._request(
            "/v1/actions",
            method="POST",
            payload={
                "action": "type",
                "target": item["id"],
                "expected_revision": input_map["revision"],
                "text": "test",
            },
        ) as response:
            action = json.loads(response.read())
        self.assertTrue(action["ok"])
        self.assertEqual(action["state_changes"][0]["type"], "window_opened")

    def test_screenshot_route_returns_image_bytes_on_demand(self):
        with self._request("/v1/screenshot?format=png") as response:
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.headers.get("X-Capture-Backend"), "fake")
            self.assertEqual(response.read(), b"image")

    def test_non_loopback_bind_requires_token(self):
        with self.assertRaises(ValueError):
            create_server(ComputerController(FakeBackend()), "0.0.0.0", 0)

    def test_bad_json_action_payload_is_rejected(self):
        request = Request(
            self.base + "/v1/actions",
            data=b"[]",
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
