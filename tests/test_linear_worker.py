import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arena_workshop.linear_api import LinearApiError
from arena_workshop.linear_worker import (
    LinearWorker,
    WorkerError,
    WindowsMutex,
    build_job_from_issue,
    stable_job_id,
)
from arena_workshop.mailbox import FileMailbox


TEAM_ID = "BlenderArena"


def make_issue(identifier="BLE-1", description=None, state="Todo"):
    return {
        "id": "issue-uuid-" + identifier,
        "identifier": identifier,
        "title": "Arena Blender job",
        "description": description if description is not None else json.dumps({
            "operation": "create_primitive",
            "parameters": {"primitive_type": "cube", "object_name": "ArenaCube"},
        }),
        "team": {"key": "BLE"},
        "state": {"name": state},
        "labels": {"nodes": [{"name": "arena-blender-job"}]},
    }


class FakeLinearApi:
    def __init__(self, issues=None):
        self.issues = {item["identifier"]: item for item in (issues or [])}
        self.comments = {item["id"]: [] for item in (issues or [])}
        self.states = {"Todo": "state-todo", "In Progress": "state-progress",
                       "Done": "state-done", "Canceled": "state-canceled"}
        self.create_comment_calls = 0
        self.fail_state_once = False
        self.fail_comments_once = False
        self.fail_create_after_save_once = False
        self.fail_issue_list_once = False

    def get_workflow_states(self, team_key):
        assert team_key == "BLE"
        return dict(self.states)

    def list_issues(self, state_name, team_key, label_name):
        assert team_key == "BLE"
        assert label_name == "arena-blender-job"
        if self.fail_issue_list_once:
            self.fail_issue_list_once = False
            raise LinearApiError("network_error", "simulated outage", retryable=True)
        return [self._copy(issue) for issue in self.issues.values()
                if issue.get("state", {}).get("name") == state_name]

    def get_issue(self, issue_id):
        for issue in self.issues.values():
            if issue["id"] == issue_id or issue["identifier"] == issue_id:
                return self._copy(issue)
        raise LinearApiError("issue_not_found", "missing")

    def list_comments(self, issue_id):
        if self.fail_comments_once:
            self.fail_comments_once = False
            raise LinearApiError("network_error", "simulated outage", retryable=True)
        return [dict(comment) for comment in self.comments.setdefault(issue_id, [])]

    def create_comment(self, issue_id, body):
        self.create_comment_calls += 1
        comment = {"id": "comment-{}".format(self.create_comment_calls), "body": body}
        self.comments.setdefault(issue_id, []).append(comment)
        if self.fail_create_after_save_once:
            self.fail_create_after_save_once = False
            raise LinearApiError("network_error", "simulated ambiguous create response", retryable=True)
        return comment

    def update_comment(self, comment_id, body):
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return dict(comment)
        raise LinearApiError("comment_not_found", "missing")

    def update_issue_state(self, issue_id, state_id):
        if self.fail_state_once:
            self.fail_state_once = False
            raise LinearApiError("network_error", "simulated lost response", retryable=True)
        state_name = next((name for name, value in self.states.items() if value == state_id), None)
        for issue in self.issues.values():
            if issue["id"] == issue_id:
                issue["state"] = {"name": state_name}
                return {"id": issue_id, "state": dict(issue["state"])}
        raise LinearApiError("issue_not_found", "missing")

    @staticmethod
    def _copy(issue):
        return json.loads(json.dumps(issue))


class LinearWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.mailbox = FileMailbox(self.root / "queue")
        self.issue = make_issue()
        self.api = FakeLinearApi([self.issue])
        self.worker = LinearWorker(self.api, mailbox=self.mailbox, clock=lambda: datetime.now(timezone.utc))
        self.worker.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_strict_json_job_validation_and_stable_id(self):
        issue = make_issue("BLE-14")
        job = build_job_from_issue(issue)
        self.assertEqual(job["job_id"], "linear-BLE-14")
        self.assertEqual(job["workspace_id"], "test")
        self.assertEqual(stable_job_id("BLE-14"), "linear-BLE-14")
        for description in (
            '{"operation":"hello_test","parameters":{"message":"ok"},"extra":1}',
            '{"operation":"hello_test","operation":"delete_object","parameters":{}}',
            '```json\n{"operation":"hello_test","parameters":{"message":"ok"}}\n```',
            '{"operation":"arbitrary_python","parameters":{}}',
            '{"operation":"create_primitive","parameters":{"primitive_type":"cube","object_name":"x","python":"bad"}}',
        ):
            with self.subTest(description=description):
                with self.assertRaises(WorkerError):
                    build_job_from_issue(make_issue(description=description))

    def test_raw_description_round_trip_preserves_escaped_names_and_vectors(self):
        description = json.dumps({
            "operation": "create_primitive",
            "parameters": {
                "primitive_type": "cube",
                "object_name": 'Village "Gate\\West',
                "location": [1.25, -2, 3],
                "rotation": [0, 45, 0],
                "scale": [1, 2, 0.5],
            },
        }, separators=(",", ":"))
        job = build_job_from_issue(make_issue(description=description))
        self.assertEqual(job["parameters"]["object_name"], 'Village "Gate\\West')
        self.assertEqual(job["parameters"]["location"], [1.25, -2, 3])
        self.assertEqual(job["parameters"]["scale"], [1, 2, 0.5])

    def test_malformed_issue_is_failed_without_mailbox_enqueue(self):
        self.issue["description"] = '{"operation":"delete_object","parameters":{},"extra":true}'
        self.assertEqual(self.worker.run_once()[-1][1], "reported_invalid")
        self.assertFalse(list(self.mailbox.inbox.glob("*.json")))
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Canceled")
        comments = self.api.list_comments(self.issue["id"])
        self.assertIn('"status": "failed"', comments[0]["body"])
        self.assertIn('"code": "invalid_job_fields"', comments[0]["body"])

    def test_duplicate_delivery_with_inbox_file_enqueues_once(self):
        self.api.fail_state_once = True
        with self.assertRaises(LinearApiError):
            self.worker.run_once()
        inbox = list(self.mailbox.inbox.glob("*.json"))
        self.assertEqual(len(inbox), 1)
        first_job = json.loads(inbox[0].read_text(encoding="utf-8"))
        self.assertEqual(first_job["job_id"], "linear-BLE-1")
        # The next delivery sees the stable mailbox artifact and only claims it.
        outcome = self.worker.run_once()
        self.assertEqual(len(list(self.mailbox.inbox.glob("*.json"))), 1)
        self.assertEqual(outcome[-1][1], "waiting_for_blender")
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "In Progress")

    def test_worker_restart_recovers_result_without_reenqueue(self):
        self.worker.run_once()
        claimed = self.mailbox.claim_next()
        self.assertEqual(claimed["job_id"], "linear-BLE-1")
        self.mailbox.write_result({
            "protocol_version": "1.0", "job_id": "linear-BLE-1", "status": "completed",
            "result": {"created": True}, "error": None, "completed_at": "2026-01-01T00:00:00Z",
        })
        restarted = LinearWorker(self.api, mailbox=self.mailbox)
        restarted.initialize()
        outcome = restarted.run_once()
        self.assertIn("reported_completed", [value for _, value in outcome])
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Done")
        self.assertFalse((self.mailbox.results / "linear-BLE-1.json").exists())
        self.assertTrue((restarted.ledger / "linear-BLE-1.json").is_file())
        self.assertEqual(self.api.create_comment_calls, 1)

    def test_terminal_ledger_prevents_redelivery_after_local_acknowledgement(self):
        self.worker.run_once()
        self.mailbox.claim_next()
        self.mailbox.write_result({
            "protocol_version": "1.0", "job_id": "linear-BLE-1", "status": "completed",
            "result": {"ok": True}, "error": None,
        })
        self.worker.run_once()
        self.assertIsNone(self.mailbox.read_result("linear-BLE-1"))
        self.issue["state"] = {"name": "Todo"}
        outcomes = self.worker.run_once()
        self.assertIn("already_reported", [value for _, value in outcomes])
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Done")
        self.assertFalse(list(self.mailbox.inbox.glob("*.json")))
        self.assertEqual(self.api.create_comment_calls, 1)

    def test_ambiguous_processing_is_reported_and_never_requeued(self):
        self.worker.run_once()
        self.mailbox.claim_next()
        processing = self.mailbox.processing / "linear-BLE-1.json"
        job = json.loads(processing.read_text(encoding="utf-8"))
        job["deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        processing.write_text(json.dumps(job), encoding="utf-8")
        outcome = self.worker.run_once()
        self.assertIn("reported_ambiguous", [value for _, value in outcome])
        self.assertTrue(processing.exists())
        self.assertFalse(list(self.mailbox.inbox.glob("*.json")))
        comment = self.api.list_comments(self.issue["id"])[0]["body"]
        self.assertIn("execution_status_ambiguous", comment)
        # Further cycles are recovery-only and cannot resubmit this job.
        self.worker.run_once()
        self.assertTrue(processing.exists())
        self.assertFalse(list(self.mailbox.inbox.glob("*.json")))

    def test_in_progress_without_any_local_record_is_ambiguous(self):
        self.issue["state"] = {"name": "In Progress"}
        self.assertIn("reported_ambiguous", [value for _, value in self.worker.run_once()])
        self.assertFalse(list(self.mailbox.inbox.glob("*.json")))
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Canceled")

    def test_network_outage_before_result_publish_preserves_mailbox(self):
        self.worker.run_once()
        self.mailbox.claim_next()
        self.mailbox.write_result({
            "protocol_version": "1.0", "job_id": "linear-BLE-1", "status": "completed",
            "result": {"ok": True}, "error": None,
        })
        self.api.fail_comments_once = True
        with self.assertRaises(LinearApiError):
            self.worker.run_once()
        self.assertTrue((self.mailbox.results / "linear-BLE-1.json").exists())
        self.assertTrue((self.mailbox.processing / "linear-BLE-1.json").exists())
        self.worker.run_once()
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Done")
        self.assertFalse((self.mailbox.results / "linear-BLE-1.json").exists())

    def test_result_comment_retry_is_idempotent_and_ack_waits_for_terminal_state(self):
        self.worker.run_once()
        self.mailbox.claim_next()
        self.mailbox.write_result({
            "protocol_version": "1.0", "job_id": "linear-BLE-1", "status": "completed",
            "result": {"ok": True}, "error": None,
        })
        self.api.fail_state_once = True
        with self.assertRaises(LinearApiError):
            self.worker.run_once()
        self.assertTrue((self.mailbox.results / "linear-BLE-1.json").exists())
        self.assertEqual(self.api.create_comment_calls, 1)
        self.worker.run_once()
        self.assertEqual(self.api.create_comment_calls, 1)
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Done")
        self.assertFalse((self.mailbox.results / "linear-BLE-1.json").exists())

    def test_ambiguous_comment_create_response_is_reconciled_without_duplicate(self):
        self.worker.run_once()
        self.mailbox.claim_next()
        self.mailbox.write_result({
            "protocol_version": "1.0", "job_id": "linear-BLE-1", "status": "completed",
            "result": {"ok": True}, "error": None,
        })
        self.api.fail_create_after_save_once = True
        with self.assertRaises(LinearApiError):
            self.worker.run_once()
        self.assertEqual(self.api.create_comment_calls, 1)
        self.worker.run_once()
        self.assertEqual(self.api.create_comment_calls, 1)
        self.assertEqual(len(self.api.list_comments(self.issue["id"])), 1)
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Done")

    def test_failure_uses_failed_state_if_configured(self):
        self.api.states["Failed"] = "state-failed"
        self.issue["description"] = '{"operation":"not_allowed","parameters":{}}'
        self.worker.initialize()
        self.worker.run_once()
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Failed")
        self.assertIn('"status": "failed"', self.api.list_comments(self.issue["id"])[0]["body"])

    def test_screenshot_reference_is_published_without_local_path(self):
        self.issue["description"] = '{"operation":"capture_screenshot","parameters":{"width":64,"height":64}}'
        uploaded = []

        def uploader(result, screenshot_root=None):
            uploaded.append((result["job_id"], screenshot_root))
            return {"status": "uploaded", "file_id": "drive-file-1",
                    "retrieval_reference": "https://drive.google.com/file/d/drive-file-1"}

        worker = LinearWorker(self.api, mailbox=self.mailbox, upload_screenshot=uploader)
        worker.initialize()
        worker.run_once()
        self.mailbox.claim_next()
        self.mailbox.write_result({
            "protocol_version": "1.0", "job_id": "linear-BLE-1", "status": "completed",
            "result": {"screenshot_path": "C:\\Users\\private\\queue\\screenshots\\linear-BLE-1.png"},
            "error": None,
        })
        worker.run_once()
        body = self.api.list_comments(self.issue["id"])[0]["body"]
        self.assertEqual(len(uploaded), 1)
        self.assertIn("drive-file-1", body)
        self.assertNotIn("C:\\\\Users", body)
        self.assertNotIn("screenshot_path", body)

    def test_failed_drive_upload_preserves_result_for_local_retry(self):
        self.issue["description"] = '{"operation":"capture_screenshot","parameters":{}}'
        worker = LinearWorker(
            self.api, mailbox=self.mailbox,
            upload_screenshot=lambda *args, **kwargs: {"status": "failed", "error": {"code": "offline"}},
        )
        worker.initialize()
        worker.run_once()
        self.mailbox.claim_next()
        self.mailbox.write_result({
            "protocol_version": "1.0", "job_id": "linear-BLE-1", "status": "completed",
            "result": {"screenshot_path": "local-only.png"}, "error": None,
        })
        worker.run_once()
        saved = self.mailbox.read_result("linear-BLE-1")
        self.assertEqual(saved["result"]["drive_upload"]["status"], "failed")
        self.assertEqual(self.api.get_issue(self.issue["id"])["state"]["name"], "Done")

        # The existing local upload-screenshot CLI can retry and update this
        # retained mailbox result; the worker then revises the same comment.
        saved["result"]["drive_upload"] = {"status": "uploaded", "file_id": "drive-retry-1"}
        self.mailbox.update_result(saved)
        worker.run_once()
        comments = self.api.list_comments(self.issue["id"])
        self.assertTrue(any("drive-retry-1" in comment["body"] for comment in comments))
        self.assertEqual(self.api.create_comment_calls, 2)  # one idempotent result revision
        self.assertIsNone(self.mailbox.read_result("linear-BLE-1"))

    def test_one_pc_only_enqueues_one_active_blender_job(self):
        second = make_issue("BLE-2")
        self.api.issues[second["identifier"]] = second
        self.api.comments[second["id"]] = []
        outcomes = self.worker.run_once()
        self.assertEqual(len(list(self.mailbox.inbox.glob("*.json"))), 1)
        self.assertEqual(self.api.get_issue("BLE-1")["state"]["name"], "In Progress")
        self.assertEqual(self.api.get_issue("BLE-2")["state"]["name"], "Todo")
        self.assertEqual(outcomes[-1][0], "BLE-1")

    def test_worker_ignores_non_todo_jobs_as_new_work(self):
        self.issue["state"] = {"name": "Backlog"}
        self.assertEqual(self.worker.run_once(), [])
        self.assertFalse(list(self.mailbox.inbox.glob("*.json")))


class MutexTests(unittest.TestCase):
    class Function:
        def __init__(self, result):
            self.result = result
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.result

    class FakeKernel:
        def __init__(self, handle):
            self.CreateMutexW = MutexTests.Function(handle)
            self.closed = []

        def CloseHandle(self, handle):
            self.closed.append(handle)

    def test_mutex_rejects_a_second_instance_and_closes_duplicate_handle(self):
        first_api = self.FakeKernel(101)
        first = WindowsMutex(kernel32=first_api, last_error=0).acquire()
        self.assertEqual(first.handle, 101)
        first.release()
        self.assertEqual(first_api.closed, [101])

        second_api = self.FakeKernel(202)
        with self.assertRaises(WorkerError) as raised:
            WindowsMutex(kernel32=second_api, last_error=WindowsMutex.ERROR_ALREADY_EXISTS).acquire()
        self.assertEqual(raised.exception.code, "worker_already_running")
        self.assertEqual(second_api.closed, [202])


if __name__ == "__main__":
    unittest.main()
