from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arena_computer_controller.controller import ComputerController
from arena_computer_controller.errors import ControllerError
from arena_computer_controller.integrations.blender import BlenderMailboxAdapter
from arena_computer_controller.server import create_server
from tests.fakes import FakeBackend


PNG = b"\x89PNG\r\n\x1a\nfake-png-payload"


class FakeJobValidationError(ValueError):
    pass


class FakeMailbox:
    def __init__(self, root):
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.processing = self.root / "processing"
        self.results = self.root / "results"
        self.rejected = self.root / "rejected"
        for directory in (self.inbox, self.processing, self.results, self.rejected):
            directory.mkdir(parents=True, exist_ok=True)
        self.enqueued = []
        self._results = {}

    def enqueue(self, job, allowed_workspaces=("test",)):
        job_id = job["job_id"]
        paths = [self.inbox, self.processing, self.results, self.rejected]
        if any((directory / f"{job_id}.json").exists() for directory in paths):
            raise FileExistsError(job_id)
        (self.inbox / f"{job_id}.json").write_text(json.dumps(job), encoding="utf-8")
        self.enqueued.append(copy.deepcopy(job))

    def read_result(self, job_id):
        value = self._results.get(job_id)
        return copy.deepcopy(value) if value is not None else None

    def finish(self, job_id, result):
        self._results[job_id] = copy.deepcopy(result)
        inbox_path = self.inbox / f"{job_id}.json"
        if inbox_path.exists():
            inbox_path.replace(self.processing / f"{job_id}.json")


class FakeWaiter:
    def __init__(self, response_factory=None):
        self.responses = deque()
        self.response_factory = response_factory or self._default_response
        self.calls = []

    @staticmethod
    def _default_response(job):
        if job["operation"] == "hello_test":
            result = {"blender_version": "5.2.0", "active_scene": "Scene", "message": job["parameters"]["message"]}
        elif job["operation"] == "capture_screenshot":
            path = Path(job["_screenshot_path"]).parent / f"{job['job_id']}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG)
            result = {
                "screenshot_path": str(path),
                "width": 640,
                "height": 480,
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "render_engine": "BLENDER_EEVEE_NEXT",
                "camera_name": "Camera",
            }
        else:
            result = {"object_name": job["parameters"].get("object_name", "ArenaCube"), "created": True}
        return {
            "protocol_version": "1.0",
            "job_id": job["job_id"],
            "status": "completed",
            "result": result,
            "error": None,
        }

    def __call__(self, mailbox, job_id, timeout):
        self.calls.append((job_id, timeout))
        if self.responses:
            response = self.responses.popleft()
            if response is None:
                return None
            if callable(response):
                response = response(mailbox.enqueued[0] if len(mailbox.enqueued) == 1 else next(
                    job for job in reversed(mailbox.enqueued) if job["job_id"] == job_id
                ))
        else:
            job = next(job for job in reversed(mailbox.enqueued) if job["job_id"] == job_id)
            response = self.response_factory(job)
        mailbox.finish(job_id, response)
        return copy.deepcopy(response)


class FakeConnector:
    SUPPORTED_OPERATIONS = (
        "hello_test", "create_test_object", "create_primitive", "move_object",
        "set_dimensions", "capture_screenshot",
    )
    JobValidationError = FakeJobValidationError

    def __init__(self, screenshot_root):
        self.screenshot_root = Path(screenshot_root)

    def create_local_job(self, operation, parameters=None):
        if operation not in FakeConnector.SUPPORTED_OPERATIONS:
            raise FakeJobValidationError("unsupported_operation")
        if parameters is None:
            parameters = {"message": "Arena Blender Workshop local add-on smoke test"} if operation == "hello_test" else {}
        if not isinstance(parameters, dict):
            raise FakeJobValidationError("parameters_must_be_object")
        if operation == "create_primitive" and not {"primitive_type", "object_name"}.issubset(parameters):
            raise FakeJobValidationError("invalid_create_primitive_parameters")
        return {
            "protocol_version": "1.0",
            "job_id": "local-fake-job",
            "workspace_id": "test",
            "operation": operation,
            "parameters": copy.deepcopy(parameters),
            "status": "queued",
            "created_at": "2026-11-06T00:00:00Z",
            "deadline": "2026-11-06T00:10:00Z",
            "result": None,
            "error": None,
            "_screenshot_path": str(self.screenshot_root / "local-fake-job.png"),
        }

    @staticmethod
    def validate_job(job, allowed_workspaces):
        if job.get("workspace_id") not in allowed_workspaces:
            raise FakeJobValidationError("unknown_workspace")
        if job.get("operation") not in FakeConnector.SUPPORTED_OPERATIONS:
            raise FakeJobValidationError("unsupported_operation")
        return copy.deepcopy(job)


class BlenderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "queue"
        self.mailbox = FakeMailbox(self.root)
        self.waiter = FakeWaiter()
        self.connector = FakeConnector(self.root / "screenshots")
        self.adapter = BlenderMailboxAdapter(
            mailbox=self.mailbox,
            connector_modules=self.connector,
            waiter=self.waiter,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_probe_sends_hello_test_and_reports_addon_version(self):
        probe = self.adapter.probe(timeout=2)
        self.assertTrue(probe["addon_responding"])
        self.assertTrue(probe["healthy"])
        self.assertEqual(probe["blender_version"], "5.2.0")
        self.assertEqual(self.mailbox.enqueued[0]["operation"], "hello_test")

    def test_command_returns_structured_result(self):
        response = self.adapter.execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "ArenaCube"},
            timeout=3,
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["result"]["object_name"], "ArenaCube")
        self.assertEqual(len(self.mailbox.enqueued), 1)

    def test_addon_reported_failure_is_not_misreported_as_success(self):
        self.waiter.response_factory = lambda job: {
            "protocol_version": "1.0",
            "job_id": job["job_id"],
            "status": "failed",
            "result": None,
            "error": {"code": "object_not_found", "message": "Object 'Missing' was not found."},
        }
        response = self.adapter.execute("move_object", {"object_name": "Missing", "location": [0, 0, 0]})
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"]["code"], "object_not_found")

    def test_timeout_returns_job_id_and_does_not_reenqueue_on_same_request_id(self):
        self.waiter.responses.append(None)
        first = self.adapter.execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "ArenaCube"},
            timeout=1,
            request_id="operation-123",
        )
        self.assertEqual(first["status"], "timeout")
        self.assertIsNotNone(first["job_id"])
        self.assertLessEqual(self.waiter.calls[0][1], 1.0)
        self.assertEqual(len(self.mailbox.enqueued), 1)

        second = self.adapter.execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "ArenaCube"},
            timeout=2,
            request_id="operation-123",
        )
        self.assertTrue(second["ok"])
        self.assertTrue(second["duplicate_request"])
        third = self.adapter.execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "ArenaCube"},
            timeout=2,
            request_id="operation-123",
        )
        self.assertTrue(third["ok"])
        self.assertTrue(third["duplicate_request"])
        self.assertEqual(len(self.mailbox.enqueued), 1)

    def test_missing_mailbox_state_after_timeout_is_never_resubmitted(self):
        self.waiter.responses.append(None)
        first = self.adapter.execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "ArenaCube"},
            timeout=1,
            request_id="ambiguous-timeout",
        )
        (self.mailbox.inbox / f"{first['job_id']}.json").unlink()
        with self.assertRaises(ControllerError) as error:
            self.adapter.execute(
                "create_primitive",
                {"primitive_type": "cube", "object_name": "ArenaCube"},
                request_id="ambiguous-timeout",
            )
        self.assertEqual(error.exception.code, "blender_job_state_ambiguous")
        self.assertEqual(len(self.mailbox.enqueued), 1)

    def test_same_idempotency_key_with_different_payload_is_rejected(self):
        self.adapter.execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "ArenaCube"},
            request_id="same-id",
        )
        with self.assertRaises(ControllerError) as error:
            self.adapter.execute(
                "create_primitive",
                {"primitive_type": "plane", "object_name": "ArenaCube"},
                request_id="same-id",
            )
        self.assertEqual(error.exception.code, "idempotency_conflict")
        self.assertEqual(len(self.mailbox.enqueued), 1)

    def test_addon_rejection_is_a_structured_failure_and_is_not_requeued(self):
        request_id = "rejected-command"
        self.waiter.responses.append(None)
        first = self.adapter.execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "ArenaCube"},
            timeout=1,
            request_id=request_id,
        )
        job_id = first["job_id"]
        (self.mailbox.inbox / f"{job_id}.json").unlink()
        (self.mailbox.rejected / f"{job_id}.json.invalid").write_text("rejected", encoding="utf-8")

        rejected = self.adapter.execute(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "ArenaCube"},
            request_id=request_id,
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["error"]["code"], "blender_job_rejected")
        self.assertEqual(len(self.mailbox.enqueued), 1)

    def test_invalid_operation_and_timeout_are_rejected(self):
        with self.assertRaises(ControllerError) as error:
            self.adapter.execute("run_python", {})
        self.assertEqual(error.exception.code, "unsupported_blender_operation")
        with self.assertRaises(ControllerError) as error:
            self.adapter.execute("create_primitive", {})
        self.assertEqual(error.exception.code, "invalid_blender_command")
        with self.assertRaises(ControllerError) as error:
            self.adapter.execute("hello_test", timeout=10000)
        self.assertEqual(error.exception.code, "invalid_timeout")

    def test_loads_connector_from_an_external_checkout(self):
        checkout = Path(self.temp_dir.name) / "separate-connector-checkout"
        package = checkout / "arena_workshop"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "protocol.py").write_text(
            """SUPPORTED_OPERATIONS = ('hello_test',)
class JobValidationError(ValueError): pass
def validate_job(job, allowed_workspaces): return job
""",
            encoding="utf-8",
        )
        (package / "mailbox.py").write_text(
            """class FileMailbox: pass
default_queue_root = lambda: '.'
""",
            encoding="utf-8",
        )
        (package / "local_smoke.py").write_text(
            """def create_local_job(operation, parameters=None): return {}
def wait_for_result(mailbox, job_id, timeout): return None
""",
            encoding="utf-8",
        )

        adapter = BlenderMailboxAdapter(checkout, mailbox=self.mailbox, waiter=self.waiter)
        self.assertEqual(adapter._connector.SUPPORTED_OPERATIONS, ("hello_test",))
        self.assertEqual(adapter.connector_root, checkout.resolve())

    def test_screenshot_is_returned_as_direct_png_bytes_not_a_local_path(self):
        response = self.adapter.execute("capture_screenshot", {"width": 640, "height": 480}, request_id="render-1")
        self.assertTrue(response["ok"])
        self.assertTrue(response["result"]["screenshot_available"])
        self.assertNotIn("screenshot_path", response["result"])
        self.assertIn("/screenshot", response["result"]["screenshot_url"])

        data, metadata = self.adapter.read_screenshot(response["job_id"])
        self.assertEqual(data, PNG)
        self.assertEqual(metadata["mime_type"], "image/png")
        self.assertEqual(metadata["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_screenshot_rejects_paths_outside_job_screenshot_directory(self):
        outside = Path(self.temp_dir.name) / "outside.png"
        outside.write_bytes(PNG)
        self.waiter.response_factory = lambda job: {
            "protocol_version": "1.0",
            "job_id": job["job_id"],
            "status": "completed",
            "result": {"screenshot_path": str(outside), "width": 1, "height": 1},
            "error": None,
        }
        response = self.adapter.execute("capture_screenshot", {}, request_id="unsafe-image")
        self.assertFalse(response["result"]["screenshot_available"])
        with self.assertRaises(ControllerError) as error:
            self.adapter.read_screenshot(response["job_id"])
        self.assertEqual(error.exception.code, "screenshot_path_rejected")


class BlenderAdapterHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "queue"
        self.mailbox = FakeMailbox(self.root)
        self.waiter = FakeWaiter()
        self.connector = FakeConnector(self.root / "screenshots")
        self.adapter = BlenderMailboxAdapter(mailbox=self.mailbox, connector_modules=self.connector, waiter=self.waiter)
        self.server = create_server(
            ComputerController(FakeBackend()), "127.0.0.1", 0, token="blender-test-token", blender_adapter=self.adapter
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def _json_request(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            self.base + path,
            data=data,
            headers={"Authorization": "Bearer blender-test-token", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_http_probe_command_and_direct_screenshot_retrieval(self):
        status, probe = self._json_request("/v1/integrations/blender/probe", {"timeout": 2})
        self.assertEqual(status, 200)
        self.assertTrue(probe["addon_responding"])

        status, command = self._json_request(
            "/v1/integrations/blender/commands",
            {
                "operation": "capture_screenshot",
                "parameters": {"width": 640, "height": 480},
                "timeout": 2,
                "request_id": "http-render-1",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(command["result"]["screenshot_available"])
        screenshot_url = self.base + command["result"]["screenshot_url"]
        request = Request(screenshot_url, headers={"Authorization": "Bearer blender-test-token"})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.headers.get("X-Blender-Job-Id"), command["job_id"])
            self.assertEqual(response.headers.get("X-Blender-Capture-Type"), "camera_render")
            self.assertEqual(response.read(), PNG)

    def test_probe_timeout_uses_gateway_timeout_status(self):
        self.waiter.responses.append(None)
        request = Request(
            self.base + "/v1/integrations/blender/probe",
            data=b'{"timeout":0.2}',
            headers={"Authorization": "Bearer blender-test-token", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 504)
        payload = json.loads(error.exception.read())
        self.assertEqual(payload["job"]["status"], "timeout")

    def test_unconfigured_blender_adapter_is_reported_cleanly(self):
        server = create_server(ComputerController(FakeBackend()), "127.0.0.1", 0, token="test")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/integrations/blender/probe",
            data=b"{}",
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
