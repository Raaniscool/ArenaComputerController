import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arena_workshop.local_smoke import create_smoke_job
from arena_workshop.mailbox import FileMailbox
from arena_workshop.protocol import JobValidationError, decode_job

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def make_job(now=None, **overrides):
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    deadline = now + timedelta(minutes=5)
    job = {
        "protocol_version": "1.0",
        "job_id": "test-job-001",
        "workspace_id": "test",
        "operation": "hello_test",
        "parameters": {"message": "Hello from Arena"},
        "status": "queued",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "deadline": deadline.isoformat().replace("+00:00", "Z"),
        "result": None,
        "error": None,
    }
    job.update(overrides)
    return job


class ProtocolTests(unittest.TestCase):
    def test_accepts_hello_job(self):
        parsed = decode_job(json.dumps(make_job(now=NOW)), {"test"}, now=NOW)
        self.assertEqual(parsed["operation"], "hello_test")

    def test_accepts_create_test_object_with_no_parameters(self):
        parsed = decode_job(json.dumps(make_job(
            now=NOW, operation="create_test_object", parameters={}
        )), {"test"}, now=NOW)
        self.assertEqual(parsed["operation"], "create_test_object")

    def test_rejects_unsupported_operation(self):
        with self.assertRaisesRegex(JobValidationError, "unsupported_operation"):
            decode_job(json.dumps(make_job(now=NOW, operation="run_python")), {"test"}, now=NOW)

    def test_rejects_unknown_workspace(self):
        with self.assertRaisesRegex(JobValidationError, "workspace_not_allowed"):
            decode_job(json.dumps(make_job(now=NOW, workspace_id="other")), {"test"}, now=NOW)

    def test_rejects_extra_fields(self):
        job = make_job(now=NOW)
        job["shell"] = "whoami"
        with self.assertRaisesRegex(JobValidationError, "invalid_job_fields"):
            decode_job(json.dumps(job), {"test"}, now=NOW)

    def test_rejects_duplicate_json_keys(self):
        raw = '{"protocol_version":"1.0","protocol_version":"1.0"}'
        with self.assertRaisesRegex(JobValidationError, "duplicate_json_key"):
            decode_job(raw, {"test"}, now=NOW)

    def test_rejects_expired_job(self):
        expired = NOW - timedelta(seconds=1)
        job = make_job(now=NOW, deadline=expired.isoformat().replace("+00:00", "Z"))
        with self.assertRaisesRegex(JobValidationError, "job_expired"):
            decode_job(json.dumps(job), {"test"}, now=NOW)

    def test_rejects_non_utc_timestamp(self):
        job = make_job(now=NOW, created_at="2026-11-03T04:00:00-08:00")
        with self.assertRaisesRegex(JobValidationError, "timestamp_must_be_utc"):
            decode_job(json.dumps(job), {"test"}, now=NOW)

    def test_rejects_oversized_job(self):
        with self.assertRaisesRegex(JobValidationError, "job_too_large"):
            decode_job(b" " * (16 * 1024 + 1), {"test"}, now=NOW)


class LocalSmokeTests(unittest.TestCase):
    def test_only_builds_allowlisted_jobs(self):
        for operation in ("hello_test", "create_test_object"):
            job = create_smoke_job(operation, now=NOW)
            self.assertEqual(job["operation"], operation)
            self.assertTrue(job["job_id"].startswith("local-"))
            self.assertEqual(decode_job(json.dumps(job), {"test"}, now=NOW), job)

    def test_rejects_unknown_operation(self):
        with self.assertRaisesRegex(ValueError, "operation must be"):
            create_smoke_job("run_python", now=NOW)


class MailboxTests(unittest.TestCase):
    def test_atomic_enqueue_claim_result_acknowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = FileMailbox(Path(tmp) / "queue")
            job = make_job()
            mailbox.enqueue(job)
            self.assertFalse(list(mailbox.inbox.glob("*.tmp")))
            claimed = mailbox.claim_next()
            self.assertEqual(claimed["job_id"], job["job_id"])
            result = {
                "protocol_version": "1.0",
                "job_id": job["job_id"],
                "status": "completed",
                "result": {"worker_status": "online"},
                "error": None,
            }
            mailbox.write_result(result)
            self.assertEqual(mailbox.read_result(job["job_id"]), result)
            mailbox.acknowledge(job["job_id"])
            self.assertIsNone(mailbox.read_result(job["job_id"]))

    def test_update_result_atomically_adds_upload_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = FileMailbox(Path(tmp) / "queue")
            result = {
                "job_id": "result-update", "status": "completed",
                "result": {"screenshot_path": "local.png"},
            }
            mailbox.write_result(result)
            result["result"]["drive_upload"] = {"status": "uploaded", "file_id": "drive-id"}
            mailbox.update_result(result)
            self.assertEqual(mailbox.read_result("result-update"), result)
            self.assertFalse(list(mailbox.results.glob("*.tmp")))

    def test_rejects_duplicate_job_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = FileMailbox(Path(tmp) / "queue")
            job = make_job()
            mailbox.enqueue(job)
            with self.assertRaises(FileExistsError):
                mailbox.enqueue(job)

    def test_malformed_inbox_job_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = FileMailbox(Path(tmp) / "queue")
            (mailbox.inbox / "bad-job.json").write_text("not json", encoding="utf-8")
            self.assertIsNone(mailbox.claim_next())
            self.assertTrue((mailbox.rejected / "bad-job.json.invalid").exists())

    def test_filename_and_payload_job_id_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = FileMailbox(Path(tmp) / "queue")
            payload = make_job(job_id="payload-job")
            (mailbox.inbox / "filename-job.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            self.assertIsNone(mailbox.claim_next())
            self.assertTrue((mailbox.rejected / "filename-job.json.invalid").exists())


if __name__ == "__main__":
    unittest.main()
