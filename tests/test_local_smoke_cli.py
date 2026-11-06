import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from arena_workshop import local_smoke


class WaitForResultTests(unittest.TestCase):
    def test_polls_until_a_successful_result_arrives(self):
        mailbox = MagicMock()
        result = {"job_id": "local-test", "status": "completed", "result": {"ok": True}}
        mailbox.read_result.side_effect = [None, result]
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(local_smoke.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(local_smoke.time, "sleep", side_effect=sleep) as sleep_mock:
            actual = local_smoke.wait_for_result(mailbox, "local-test", timeout=1.0)

        self.assertEqual(actual, result)
        self.assertEqual(mailbox.read_result.call_count, 2)
        sleep_mock.assert_called_once_with(local_smoke._POLL_INTERVAL_SECONDS)

    def test_returns_none_when_timeout_expires(self):
        mailbox = MagicMock()
        mailbox.read_result.return_value = None
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(local_smoke.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(local_smoke.time, "sleep", side_effect=sleep) as sleep_mock:
            actual = local_smoke.wait_for_result(mailbox, "local-test", timeout=0.5)

        self.assertIsNone(actual)
        self.assertEqual(mailbox.read_result.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [0.25, 0.25])


class CliWaitTests(unittest.TestCase):
    def setUp(self):
        self.mailbox = MagicMock()
        self.mailbox.results = Path("test-results")
        self.stdout = io.StringIO()
        self.factory_patcher = patch.object(
            local_smoke, "FileMailbox", return_value=self.mailbox
        )
        self.root_patcher = patch.object(local_smoke, "default_queue_root", return_value=Path("test-queue"))
        self.factory = self.factory_patcher.start()
        self.root_patcher.start()
        self.addCleanup(self.factory_patcher.stop)
        self.addCleanup(self.root_patcher.stop)

    def run_cli(self, args):
        with redirect_stdout(self.stdout):
            return local_smoke.main(args)

    def test_wait_prints_successful_result_and_enqueues_only_once(self):
        result = {"job_id": "local-test", "status": "completed", "result": {"ok": True}}
        self.mailbox.read_result.side_effect = [None, result]
        with patch.object(local_smoke.time, "sleep", return_value=None):
            exit_code = self.run_cli(["create_test_object", "--wait", "--timeout", "5"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), result)
        self.mailbox.enqueue.assert_called_once()
        self.assertEqual(self.mailbox.read_result.call_count, 2)
        self.factory.assert_called_once_with(Path("test-queue"))

    def test_failed_job_is_printed_and_returns_nonzero(self):
        result = {
            "job_id": "local-test",
            "status": "failed",
            "error": {"code": "blender_operation_failed"},
        }
        self.mailbox.read_result.return_value = result

        exit_code = self.run_cli(["hello_test", "--wait"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(self.stdout.getvalue()), result)
        self.mailbox.enqueue.assert_called_once()

    def test_timeout_is_clear_and_does_not_resubmit(self):
        with patch.object(local_smoke, "wait_for_result", return_value=None) as wait_mock:
            exit_code = self.run_cli(["create_test_object", "--wait", "--timeout", "2.5"])

        output = json.loads(self.stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(output["status"], "timeout")
        self.assertEqual(output["timeout_seconds"], 2.5)
        self.assertIn("not resubmitted", output["message"])
        self.mailbox.enqueue.assert_called_once()
        wait_mock.assert_called_once_with(self.mailbox, self.mailbox.enqueue.call_args.args[0]["job_id"], 2.5)

    def test_default_behavior_does_not_wait_or_read_a_result(self):
        with patch.object(local_smoke, "wait_for_result") as wait_mock:
            exit_code = self.run_cli(["hello_test"])

        output = json.loads(self.stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(set(output), {"job_id", "result_path"})
        self.assertEqual(
            Path(output["result_path"]), Path("test-results") / (output["job_id"] + ".json")
        )
        self.mailbox.enqueue.assert_called_once()
        self.mailbox.read_result.assert_not_called()
        wait_mock.assert_not_called()

    def test_screenshot_command_enqueues_bounded_resolution(self):
        self.mailbox.read_result.return_value = {
            "job_id": "local-shot", "status": "completed",
            "result": {"screenshot_path": "queue/screenshots/local-shot.png"},
        }
        with patch.object(local_smoke, "_upload_completed_screenshot",
                          return_value=({"status": "uploaded"}, True)) as upload_mock:
            exit_code = self.run_cli([
                "capture_screenshot", "--width", "320", "--height", "240", "--wait",
            ])
        self.assertEqual(exit_code, 0)
        upload_mock.assert_called_once()
        job = self.mailbox.enqueue.call_args.args[0]
        self.assertEqual(job["operation"], "capture_screenshot")
        self.assertEqual(job["parameters"], {"width": 320, "height": 240})

    def test_screenshot_upload_failure_is_reported_without_changing_capture_success(self):
        result = {
            "job_id": "local-shot", "status": "completed",
            "result": {"screenshot_path": "queue/screenshots/local-shot.png"},
        }
        self.mailbox.read_result.return_value = result
        failure = {
            "status": "failed", "job_id": "local-shot", "file_id": None,
            "error": {"code": "authorization_required", "message": "Run authorize-drive."},
        }
        with patch.object(local_smoke, "_upload_completed_screenshot", return_value=(failure, True)):
            exit_code = self.run_cli(["capture_screenshot", "--wait"])
        output = json.loads(self.stdout.getvalue())
        self.assertEqual(exit_code, 3)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["result"]["drive_upload"]["error"]["code"], "authorization_required")

    def test_modeling_command_enqueues_validated_parameters(self):
        self.mailbox.read_result.return_value = {
            "job_id": "local-test", "status": "completed", "result": {"created": True}
        }
        exit_code = self.run_cli([
            "create_primitive", "--primitive-type", "cube", "--object-name", "CliCube",
            "--location", "1", "2", "3", "--rotation", "0", "0", "45",
            "--scale", "2", "2", "2", "--wait",
        ])

        self.assertEqual(exit_code, 0)
        job = self.mailbox.enqueue.call_args.args[0]
        self.assertEqual(job["operation"], "create_primitive")
        self.assertEqual(job["parameters"], {
            "primitive_type": "cube", "object_name": "CliCube",
            "location": [1.0, 2.0, 3.0], "rotation": [0.0, 0.0, 45.0],
            "scale": [2.0, 2.0, 2.0],
        })
        self.mailbox.enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
