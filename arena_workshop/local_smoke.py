"""Restricted local CLI for queueing validated Blender mailbox jobs."""

import argparse
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone

from .mailbox import FileMailbox, default_queue_root
from .protocol import JobValidationError, SUPPORTED_OPERATIONS, validate_job

_DEFAULT_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.25
LOCAL_OPERATIONS = SUPPORTED_OPERATIONS


def _new_job(operation, parameters, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    job = {
        "protocol_version": "1.0",
        "job_id": "local-" + uuid.uuid4().hex,
        "workspace_id": "test",
        "operation": operation,
        "parameters": parameters,
        "status": "queued",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "deadline": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "result": None,
        "error": None,
    }
    return validate_job(job, {"test"}, now=now)


def create_smoke_job(operation, now=None):
    """Build one of the original fixed smoke tests; accept no custom payload."""
    if operation == "hello_test":
        parameters = {"message": "Arena Blender Workshop local add-on smoke test"}
    elif operation == "create_test_object":
        parameters = {}
    else:
        raise ValueError("operation must be hello_test or create_test_object")
    return _new_job(operation, parameters, now=now)


def create_local_job(operation, parameters=None, now=None):
    """Build and validate one allowlisted test or modeling job."""
    if operation not in LOCAL_OPERATIONS:
        raise JobValidationError("unsupported_operation")
    if operation == "hello_test" and parameters is None:
        parameters = {"message": "Arena Blender Workshop local add-on smoke test"}
    elif parameters is None:
        parameters = {}
    return _new_job(operation, parameters, now=now)


def _positive_seconds(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds")
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite number greater than zero")
    return seconds


def wait_for_result(mailbox, job_id, timeout, poll_interval=_POLL_INTERVAL_SECONDS):
    """Poll one already-enqueued job until a result arrives or the timeout expires."""
    deadline = time.monotonic() + timeout
    while True:
        result = mailbox.read_result(job_id)
        if result is not None:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval, remaining))


def _upload_completed_screenshot(result, mailbox, screenshot_root):
    """Upload only the screenshot referenced by a completed capture job result."""
    from .drive import screenshot_result_upload

    operation_result = result.get("result") if isinstance(result, dict) else None
    previous = operation_result.get("drive_upload") if isinstance(operation_result, dict) else None
    if isinstance(previous, dict) and previous.get("status") == "uploaded":
        return previous, True

    upload_result = screenshot_result_upload(result, screenshot_root=screenshot_root)
    if isinstance(operation_result, dict):
        operation_result["drive_upload"] = upload_result
    else:
        result["drive_upload"] = upload_result
    try:
        mailbox.update_result(result)
        return upload_result, True
    except Exception:
        return upload_result, False


def _build_cli_parameters(args):
    params = {}
    if args.operation == "hello_test":
        params["message"] = "Arena Blender Workshop local add-on smoke test"

    for field in ("primitive_type", "object_name", "location", "rotation", "scale",
                  "new_name", "material_name", "base_color", "roughness", "metallic",
                  "dimensions", "width", "height"):
        value = getattr(args, field)
        if value is not None:
            params[field] = value
    return params


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Queue a validated, allowlisted Blender add-on test or modeling job"
    )
    parser.add_argument("operation", choices=LOCAL_OPERATIONS)
    parser.add_argument("--primitive-type", choices=("cube", "uv_sphere", "cylinder", "plane", "cone"))
    parser.add_argument("--object-name")
    parser.add_argument("--location", nargs=3, type=float, metavar=("X", "Y", "Z"))
    parser.add_argument("--rotation", nargs=3, type=float, metavar=("X", "Y", "Z"),
                        help="Euler rotation in degrees")
    parser.add_argument("--scale", nargs=3, type=float, metavar=("X", "Y", "Z"))
    parser.add_argument("--dimensions", nargs=3, type=float, metavar=("X", "Y", "Z"),
                        help="set mesh object dimensions in Blender units")
    parser.add_argument("--width", type=int, help="screenshot width (64-1280, default 640)")
    parser.add_argument("--height", type=int, help="screenshot height (64-720, default 480)")
    parser.add_argument("--new-name")
    parser.add_argument("--material-name")
    parser.add_argument("--base-color", nargs=4, type=float, metavar=("R", "G", "B", "A"),
                        help="linear RGBA components from 0 to 1")
    parser.add_argument("--roughness", type=float)
    parser.add_argument("--metallic", type=float)
    parser.add_argument(
        "--wait",
        action="store_true",
        help="wait for Blender to write the result and print it as formatted JSON",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=_DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="maximum time to wait with --wait (default: 60 seconds)",
    )
    args = parser.parse_args(argv)

    try:
        job = create_local_job(args.operation, _build_cli_parameters(args))
    except JobValidationError as exc:
        parser.error("invalid operation parameters: {}".format(exc))

    mailbox = FileMailbox(default_queue_root())
    # Enqueue exactly once. Waiting only polls the result for this job ID.
    mailbox.enqueue(job)
    result_path = mailbox.results / (job["job_id"] + ".json")

    if not args.wait:
        print(json.dumps({"job_id": job["job_id"], "result_path": str(result_path)}))
        return 0

    result = wait_for_result(mailbox, job["job_id"], args.timeout)
    if result is None:
        print(json.dumps({
            "job_id": job["job_id"],
            "status": "timeout",
            "timeout_seconds": args.timeout,
            "result_path": str(result_path),
            "message": "No Blender result arrived before the timeout; the job was not resubmitted.",
        }, indent=2))
        return 2

    upload_status = None
    upload_recorded = True
    if (args.operation == "capture_screenshot" and isinstance(result, dict)
            and result.get("status") == "completed"):
        upload_status, upload_recorded = _upload_completed_screenshot(
            result, mailbox, mailbox.root / "screenshots",
        )
        if isinstance(result.get("result"), dict) and isinstance(upload_status, dict):
            result["result"].setdefault("drive_upload", upload_status)
        if not upload_recorded:
            result["drive_upload_record_error"] = {
                "code": "result_update_failed",
                "message": "The PNG upload completed or failed, but its status could not be saved to the local mailbox result.",
            }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not isinstance(result, dict) or result.get("status") != "completed":
        return 1
    if isinstance(upload_status, dict) and upload_status.get("status") != "uploaded":
        return 3
    if not upload_recorded:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
