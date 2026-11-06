"""Single-instance, outbound-only Linear worker for the local Blender mailbox."""

import ctypes
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .linear_api import LinearApiError
from .mailbox import FileMailbox, default_queue_root
from .protocol import (
    JobValidationError,
    SUPPORTED_OPERATIONS,
    parse_job_description,
    validate_job,
)

_LOG = logging.getLogger("arena_blender_workshop.linear_worker")
TEAM_KEY = "BLE"
QUEUE_LABEL = "arena-blender-job"
WORKSPACE_ID = "test"
PROTOCOL_VERSION = "1.0"
RESULT_MARKER_PREFIX = "ARENA_BLENDER_RESULT:"
ISSUE_ID_PATTERN = re.compile(r"^BLE-[1-9][0-9]*$")
MAX_COMMENT_BYTES = 64 * 1024
DEFAULT_JOB_MINUTES = 10
PROCESSING_POLL_SECONDS = 5.0


class WorkerError(RuntimeError):
    """Safe worker failure with no issue payload or credential content."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def stable_job_id(identifier):
    if not isinstance(identifier, str) or not ISSUE_ID_PATTERN.fullmatch(identifier):
        raise WorkerError("invalid_issue_identifier", "The issue identifier is not a BlenderArena job ID.")
    return "linear-" + identifier


def build_job_from_issue(issue, now=None):
    """Validate strict issue-description JSON and create a local protocol v1 job."""
    team = issue.get("team") if isinstance(issue, dict) else None
    if not isinstance(team, dict) or team.get("key") != TEAM_KEY:
        raise WorkerError("wrong_team", "Only BlenderArena (BLE) issues are eligible jobs.")
    identifier = issue.get("identifier")
    job_id = stable_job_id(identifier)
    description = issue.get("description")
    try:
        request = parse_job_description(description)
    except JobValidationError as exc:
        code = str(exc)
        if code == "job_too_large":
            raise WorkerError("job_too_large", "The Blender job request exceeds the protocol size limit.") from exc
        if code == "invalid_utf8":
            raise WorkerError("invalid_job_json", "The issue description is not valid UTF-8 JSON.") from exc
        if code in {"invalid_json", "duplicate_json_key", "non_finite_json_number", "description_must_be_text"}:
            raise WorkerError(
                "invalid_job_json",
                "The issue description must be one strict JSON object with no duplicate keys.",
            ) from exc
        if code == "invalid_job_fields":
            raise WorkerError("invalid_job_fields", "The JSON request must contain exactly operation and parameters.") from exc
        if code == "parameters_must_be_object":
            raise WorkerError("invalid_parameters", "The operation parameters must be a JSON object.") from exc
        raise WorkerError("invalid_job_json", "The issue description is not a valid job request.") from exc
    if not isinstance(request["operation"], str) or request["operation"] not in SUPPORTED_OPERATIONS:
        raise WorkerError("unsupported_operation", "The requested Blender operation is not allowlisted.")

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    job = {
        "protocol_version": PROTOCOL_VERSION,
        "job_id": job_id,
        "workspace_id": WORKSPACE_ID,
        "operation": request["operation"],
        "parameters": request["parameters"],
        "status": "queued",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "deadline": (now + timedelta(minutes=DEFAULT_JOB_MINUTES)).isoformat().replace("+00:00", "Z"),
        "result": None,
        "error": None,
    }
    try:
        return validate_job(job, {WORKSPACE_ID}, now=now)
    except JobValidationError as exc:
        code = str(exc)
        raise WorkerError("invalid_job_parameters", "The request failed protocol validation ({}).".format(code)) from exc


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_result(result):
    """Remove local filesystem paths before placing a result in a cloud comment."""
    if isinstance(result, dict):
        return {key: _safe_result(value) for key, value in result.items() if key != "screenshot_path"}
    if isinstance(result, list):
        return [_safe_result(value) for value in result]
    return result


def _read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".linear-state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            pass


class WindowsMutex:
    """Named Windows mutex preventing multiple local worker processes."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name="Global\\ArenaBlenderWorkshop.LinearWorker", kernel32=None, last_error=None):
        self.name = name
        self._kernel32 = kernel32
        self._last_error = last_error
        self.handle = None

    def acquire(self):
        self._kernel32, self.handle = acquire_worker_mutex(
            self.name, kernel32=self._kernel32, last_error=self._last_error,
        )
        return self

    def release(self):
        if self.handle is None:
            return
        self._kernel32.CloseHandle(self.handle)
        self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False


class LinearWorker:
    """Reconcile a single Linear team queue with the existing atomic mailbox."""

    def __init__(self, api, mailbox=None, upload_screenshot=None, state_root=None, clock=None):
        self.api = api
        self.mailbox = mailbox or FileMailbox(default_queue_root())
        self.upload_screenshot = upload_screenshot
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state_root = Path(state_root) if state_root is not None else self.mailbox.root / "linear_worker"
        self.ledger = self.state_root / "reported"
        self.states = None
        self.failed_state_name = None

    def initialize(self):
        states = self.api.get_workflow_states(TEAM_KEY)
        required = {"Todo", "In Progress", "Done", "Canceled"}
        missing = required - set(states)
        if missing:
            raise WorkerError("workflow_state_missing", "The BlenderArena team is missing a required workflow state.")
        self.failed_state_name = "Failed" if "Failed" in states else "Canceled"
        self.states = states

    def _local_paths(self, job_id):
        filename = job_id + ".json"
        return {
            "inbox": self.mailbox.inbox / filename,
            "processing": self.mailbox.processing / filename,
            "result": self.mailbox.results / filename,
            "rejected": self.mailbox.rejected / (filename + ".invalid"),
            "ledger": self.ledger / (job_id + ".json"),
        }

    def _local_job_ids(self):
        found = set()
        for directory in (self.mailbox.inbox, self.mailbox.processing, self.mailbox.results):
            for path in directory.glob("linear-BLE-*.json"):
                job_id = path.stem
                try:
                    stable_job_id(job_id[len("linear-"):])
                except WorkerError:
                    continue
                found.add(job_id)
        for path in self.mailbox.rejected.glob("linear-BLE-*.json.invalid"):
            job_id = path.name[:-len(".json.invalid")]
            try:
                stable_job_id(job_id[len("linear-"):])
            except WorkerError:
                continue
            found.add(job_id)
        return found

    def _ledger_entry(self, job_id):
        value = _read_json_file(self._local_paths(job_id)["ledger"])
        if not isinstance(value, dict) or value.get("job_id") != job_id:
            return None
        if value.get("status") not in {"completed", "failed"}:
            return None
        if value.get("linear_state") not in {"Done", "Failed", "Canceled"}:
            return None
        return value

    def _save_ledger(self, job_id, status, linear_state):
        _atomic_json(self._local_paths(job_id)["ledger"], {
            "job_id": job_id,
            "status": status,
            "linear_state": linear_state,
            "reported_at": _utc_now(),
        })

    def _labels_include_queue(self, issue):
        labels = issue.get("labels") if isinstance(issue, dict) else None
        nodes = labels.get("nodes") if isinstance(labels, dict) else None
        return isinstance(nodes, list) and any(
            isinstance(label, dict) and label.get("name") == QUEUE_LABEL for label in nodes
        )

    def _eligible(self, issue, state_name=None):
        team = issue.get("team") if isinstance(issue, dict) else None
        state_info = issue.get("state") if isinstance(issue, dict) else None
        if not isinstance(team, dict) or team.get("key") != TEAM_KEY:
            return False
        state = state_info.get("name") if isinstance(state_info, dict) else None
        if state_name is not None and state != state_name:
            return False
        if not self._labels_include_queue(issue):
            return False
        return True

    def _result_body(self, job_id, issue, report):
        content = {
            "job_id": job_id,
            "issue": issue.get("identifier"),
            "status": report.get("status"),
            "result": _safe_result(report.get("result")) if report.get("result") is not None else None,
            "error": _safe_result(report.get("error")) if report.get("error") is not None else None,
        }
        if isinstance(report.get("completed_at"), str):
            content["completed_at"] = report["completed_at"]
        serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        marker = RESULT_MARKER_PREFIX + job_id + ":" + digest
        body = marker + "\n" + serialized
        if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
            raise WorkerError("result_too_large", "The structured result is too large to publish safely to Linear.")
        return marker, body

    def _upsert_result_comment(self, job_id, issue, report):
        marker, body = self._result_body(job_id, issue, report)
        comments = self.api.list_comments(issue["id"])
        existing = [comment for comment in comments if isinstance(comment, dict)
                    and isinstance(comment.get("body"), str)
                    and comment["body"].splitlines()[:1] == [marker]]
        if existing and not any(comment.get("body") == body for comment in existing):
            raise LinearApiError("result_comment_conflict", "A result comment marker exists with different content.")
        if not existing:
            # Comments are create-only under the minimal comments:create scope.
            # The content hash makes retries idempotent; a deliberately changed
            # result (for example, after a manual Drive retry) gets a new revision.
            self.api.create_comment(issue["id"], body)

        # Read-after-write confirms durability and resolves an ambiguous response
        # without requiring the broader comments:update permission.
        confirmed = self.api.list_comments(issue["id"])
        if not any(isinstance(comment, dict) and comment.get("body") == body for comment in confirmed):
            raise LinearApiError("comment_not_durable", "Linear did not return the saved result comment.", retryable=True)

    def _set_state_confirmed(self, issue, state_name):
        state_id = self.states.get(state_name) if self.states else None
        if not isinstance(state_id, str):
            raise WorkerError("workflow_state_missing", "The required Linear workflow state is not configured.")
        self.api.update_issue_state(issue["id"], state_id)
        refreshed = self.api.get_issue(issue["id"])
        if refreshed.get("state", {}).get("name") != state_name:
            raise LinearApiError("state_not_durable", "Linear did not return the saved terminal issue state.", retryable=True)

    def _finalize(self, issue, job_id, report, keep_local=False):
        status = report.get("status")
        if status not in {"completed", "failed"}:
            raise WorkerError("invalid_result_status", "The worker result has no valid terminal status.")
        target_state = "Done" if status == "completed" else self.failed_state_name
        self._upsert_result_comment(job_id, issue, report)
        self._set_state_confirmed(issue, target_state)
        self._save_ledger(job_id, status, target_state)
        if not keep_local:
            self.mailbox.acknowledge(job_id)
        return target_state

    def _failure_report(self, job_id, code, message):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "status": "failed",
            "result": None,
            "error": {"code": code, "message": message},
            "completed_at": _utc_now(),
        }

    def _read_local_result(self, path):
        result = _read_json_file(path)
        if not isinstance(result, dict) or result.get("status") not in {"completed", "failed"}:
            return None
        return result

    def _read_local_job(self, paths):
        for key in ("processing", "inbox"):
            job = _read_json_file(paths[key])
            if isinstance(job, dict) and job.get("job_id"):
                return job
        return None

    def _upload_screenshot_if_needed(self, job, result):
        if (not isinstance(job, dict) or job.get("operation") != "capture_screenshot"
                or result.get("status") != "completed"):
            return result
        operation_result = result.get("result")
        if not isinstance(operation_result, dict):
            return result
        previous = operation_result.get("drive_upload")
        if isinstance(previous, dict) and previous.get("status") in {"uploaded", "failed"}:
            return result
        if self.upload_screenshot is None:
            from .drive import screenshot_result_upload
            upload = screenshot_result_upload
        else:
            upload = self.upload_screenshot
        try:
            upload_result = upload(result, screenshot_root=self.mailbox.root / "screenshots")
        except Exception as exc:
            upload_result = {
                "status": "failed",
                "job_id": result.get("job_id"),
                "error": {"code": "drive_upload_failed", "message": "Screenshot upload failed ({}).".format(type(exc).__name__)},
            }
        operation_result["drive_upload"] = upload_result
        self.mailbox.update_result(result)
        return result

    def _reconcile_existing(self, issue, allow_recover_state=True):
        job_id = stable_job_id(issue.get("identifier"))
        paths = self._local_paths(job_id)
        ledger = self._ledger_entry(job_id)
        if ledger is not None:
            target = ledger["linear_state"]
            current = issue.get("state", {}).get("name")
            if current != target:
                self._set_state_confirmed(issue, target)
            local_result = self._read_local_result(paths["result"])
            if local_result is not None:
                upload = (local_result.get("result") or {}).get("drive_upload") if isinstance(local_result.get("result"), dict) else None
                if not (isinstance(upload, dict) and upload.get("status") == "failed"):
                    # This also completes cleanup after a restart between the
                    # durable Linear report and mailbox acknowledgement.
                    self._finalize(issue, job_id, local_result)
                    return "reported_" + local_result["status"]
            return "already_reported"

        local_result = self._read_local_result(paths["result"])
        if local_result is not None:
            job = self._read_local_job(paths)
            local_result = self._upload_screenshot_if_needed(job, local_result)
            upload = (local_result.get("result") or {}).get("drive_upload") if isinstance(local_result.get("result"), dict) else None
            preserve_for_drive_retry = (
                isinstance(job, dict) and job.get("operation") == "capture_screenshot"
                and isinstance(upload, dict) and upload.get("status") == "failed"
            )
            self._finalize(issue, job_id, local_result, keep_local=preserve_for_drive_retry)
            return "reported_" + local_result["status"]

        if paths["rejected"].exists():
            report = self._failure_report(
                job_id, "mailbox_rejected", "The local mailbox rejected the validated job; no automatic resubmission was attempted.",
            )
            self._finalize(issue, job_id, report)
            return "reported_failed"

        if paths["processing"].is_file():
            local_job = self._read_local_job(paths)
            deadline = None
            if isinstance(local_job, dict):
                try:
                    raw = local_job.get("deadline")
                    deadline = datetime.fromisoformat(raw[:-1] + "+00:00" if isinstance(raw, str) and raw.endswith("Z") else raw)
                except (TypeError, ValueError):
                    deadline = None
            now = self.clock()
            if deadline is None or deadline.tzinfo is None:
                report = self._failure_report(
                    job_id, "execution_status_ambiguous",
                    "The local processing record has no trustworthy deadline. The job was not retried; inspect Blender and the scene before resubmitting.",
                )
                self._finalize(issue, job_id, report, keep_local=True)
                return "reported_ambiguous"
            if now.astimezone(timezone.utc) >= deadline.astimezone(timezone.utc):
                report = self._failure_report(
                    job_id, "execution_status_ambiguous",
                    "Blender claimed this job but no result arrived before its deadline. The job was not retried; inspect Blender and the scene before resubmitting.",
                )
                self._finalize(issue, job_id, report, keep_local=True)
                return "reported_ambiguous"
            if allow_recover_state and issue.get("state", {}).get("name") != "In Progress":
                self._set_state_confirmed(issue, "In Progress")
            return "waiting_for_blender"

        if paths["inbox"].is_file():
            local_job = _read_json_file(paths["inbox"])
            if isinstance(local_job, dict):
                try:
                    deadline_raw = local_job.get("deadline")
                    deadline = datetime.fromisoformat(
                        deadline_raw[:-1] + "+00:00" if isinstance(deadline_raw, str) and deadline_raw.endswith("Z") else deadline_raw
                    )
                except (TypeError, ValueError):
                    deadline = None
                if deadline is None or deadline.tzinfo is None:
                    report = self._failure_report(
                        job_id, "invalid_local_job", "The local queued record has no valid deadline and was not resubmitted.",
                    )
                    self._finalize(issue, job_id, report, keep_local=True)
                    return "reported_invalid_local"
                if self.clock().astimezone(timezone.utc) >= deadline.astimezone(timezone.utc):
                    report = self._failure_report(
                        job_id, "job_expired_before_claim", "The queued job expired before Blender claimed it; it was not resubmitted.",
                    )
                    self._finalize(issue, job_id, report, keep_local=True)
                    return "reported_expired"
            if allow_recover_state and issue.get("state", {}).get("name") != "In Progress":
                self._set_state_confirmed(issue, "In Progress")
            return "waiting_for_blender"

        if issue.get("state", {}).get("name") == "In Progress":
            report = self._failure_report(
                job_id, "execution_status_ambiguous",
                "Linear shows this job as processing, but no local mailbox record exists. It was not re-enqueued; inspect Blender and the queue before resubmitting.",
            )
            self._finalize(issue, job_id, report, keep_local=True)
            return "reported_ambiguous"
        return "no_local_work"

    def _process_pending_issue(self, listed_issue):
        issue_id = listed_issue.get("id")
        if not isinstance(issue_id, str):
            raise LinearApiError("invalid_linear_response", "Linear returned a job without an issue ID.")
        issue = self.api.get_issue(issue_id)
        if not self._eligible(issue, "Todo"):
            return "no_longer_pending"
        job_id = stable_job_id(issue.get("identifier"))
        ledger = self._ledger_entry(job_id)
        if ledger is not None:
            target = ledger["linear_state"]
            if issue.get("state", {}).get("name") != target:
                self._set_state_confirmed(issue, target)
            return "already_reported"

        paths = self._local_paths(job_id)
        if any(paths[key].exists() for key in ("inbox", "processing", "result", "rejected")):
            # A previous worker may have written the mailbox job and stopped
            # before changing Linear state. Resume using the exact stable ID.
            self._set_state_confirmed(issue, "In Progress")
            issue["state"] = {"name": "In Progress"}
            return self._reconcile_existing(issue)

        try:
            job = build_job_from_issue(issue, now=self.clock())
        except WorkerError as exc:
            report = self._failure_report(job_id, exc.code, exc.message)
            self._finalize(issue, job_id, report)
            return "reported_invalid"

        try:
            self.mailbox.enqueue(job, allowed_workspaces={WORKSPACE_ID})
        except FileExistsError:
            # Another delivery attempt or a crash-recovered artifact won the
            # enqueue race; never manufacture a second job ID.
            if not any(paths[key].exists() for key in ("inbox", "processing", "result", "rejected")):
                raise
        self._set_state_confirmed(issue, "In Progress")
        issue["state"] = {"name": "In Progress"}
        return self._reconcile_existing(issue)

    def run_once(self):
        if self.states is None:
            self.initialize()
        outcomes = []
        active_job = False
        seen = set()
        # Only query active Linear issues continuously; completed history is
        # reconciled by looking up local mailbox IDs left behind by a crash.
        for issue in self.api.list_issues("In Progress", TEAM_KEY, QUEUE_LABEL):
            if not self._eligible(issue, "In Progress"):
                continue
            job_id = stable_job_id(issue.get("identifier"))
            seen.add(job_id)
            fresh = self.api.get_issue(issue["id"])
            outcome = self._reconcile_existing(fresh)
            outcomes.append((fresh.get("identifier"), outcome))
            if outcome == "waiting_for_blender":
                active_job = True

        for job_id in sorted(self._local_job_ids() - seen):
            issue = self.api.get_issue(job_id[len("linear-"):])
            current_state = issue.get("state", {}).get("name")
            if current_state in {"In Progress", "Done", "Failed", "Canceled"}:
                outcome = self._reconcile_existing(issue)
                outcomes.append((issue.get("identifier"), outcome))
                if outcome == "waiting_for_blender":
                    active_job = True

        if active_job:
            return outcomes
        pending = self.api.list_issues("Todo", TEAM_KEY, QUEUE_LABEL)
        for issue in pending:
            if not self._eligible(issue, "Todo"):
                continue
            outcome = self._process_pending_issue(issue)
            outcomes.append((issue.get("identifier"), outcome))
            # One PC/Blender instance gets one active job. Leave later Todo
            # issues untouched until this mailbox entry is terminal.
            if outcome == "waiting_for_blender":
                break
        return outcomes

    def run_forever(self, poll_interval=PROCESSING_POLL_SECONDS, sleep=time.sleep, stop=None):
        delay = poll_interval
        while stop is None or not stop():
            try:
                if getattr(self.api, "_access_token", None) is None and self.states is not None:
                    self.api.authenticate()
                self.run_once()
                delay = poll_interval
            except (LinearApiError, WorkerError, OSError) as exc:
                code = getattr(exc, "code", "worker_error")
                _LOG.warning("Linear worker cycle deferred (%s).", code)
                delay = min(max(poll_interval, delay * 2), 60.0)
            sleep(delay)


def _win32_mutex_functions(kernel32=None):
    """Small seam for tests; production uses the real Windows kernel API."""
    if kernel32 is not None:
        return kernel32
    if os.name != "nt":
        raise WorkerError("windows_only", "The Linear worker requires Windows and a Windows named mutex.")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def acquire_worker_mutex(name="Global\\ArenaBlenderWorkshop.LinearWorker", kernel32=None, last_error=None):
    """Acquire a named mutex; exposed separately to test duplicate startup safely."""
    kernel32 = _win32_mutex_functions(kernel32)
    from ctypes import wintypes
    create_mutex = kernel32.CreateMutexW
    try:
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
    except AttributeError:
        pass
    if last_error is None and hasattr(ctypes, "set_last_error"):
        ctypes.set_last_error(0)
    handle = create_mutex(None, False, name)
    if not handle:
        raise WorkerError("mutex_create_failed", "Windows could not create the worker mutex.")
    error = ctypes.get_last_error() if last_error is None else last_error
    if error == WindowsMutex.ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise WorkerError("worker_already_running", "Another Linear worker instance is already running.")
    return kernel32, handle
