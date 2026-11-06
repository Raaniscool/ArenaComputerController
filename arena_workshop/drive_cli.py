"""CLI commands for local Google Drive authorization and screenshot uploads."""

import argparse
import json

from .drive import (
    authorization_result,
    default_oauth_config_path,
    screenshot_result_upload,
)
from .mailbox import FileMailbox, default_queue_root
from .protocol import JobValidationError


def _print(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def authorize_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m arena_workshop authorize-drive",
        description="Authorize this Windows user to upload Arena Blender screenshots to Google Drive.",
    )
    parser.add_argument(
        "--config", default=str(default_oauth_config_path()),
        help="local Desktop OAuth JSON path (default: google_oauth_client.json in the repository root)",
    )
    parser.add_argument("--timeout", type=int, default=180, help="authorization callback timeout in seconds")
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.timeout > 900:
        parser.error("--timeout must be between 1 and 900 seconds")
    result = authorization_result(config_path=args.config, timeout=args.timeout)
    _print(result)
    if result.get("status") != "authorized":
        return 1
    return 0


def upload_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m arena_workshop upload-screenshot",
        description="Upload the local PNG recorded for one completed Blender screenshot job.",
    )
    parser.add_argument("--job-id", required=True, help="job ID printed by the local Blender CLI")
    parser.add_argument(
        "--config", default=str(default_oauth_config_path()),
        help="local Desktop OAuth JSON path (default: google_oauth_client.json in the repository root)",
    )
    args = parser.parse_args(argv)
    mailbox = FileMailbox(default_queue_root())
    try:
        result = mailbox.read_result(args.job_id)
    except JobValidationError:
        _print({
            "status": "failed", "job_id": None,
            "error": {"code": "invalid_job_id", "message": "The supplied job ID is invalid."},
        })
        return 1
    if result is None:
        failure = {
            "status": "failed", "job_id": args.job_id,
            "error": {"code": "job_result_missing", "message": "No local result exists for this job ID."},
        }
        _print(failure)
        return 1
    operation_result = result.get("result") if isinstance(result, dict) else None
    if isinstance(operation_result, dict):
        previous = operation_result.get("drive_upload")
        if isinstance(previous, dict) and previous.get("status") == "uploaded":
            _print(previous)
            return 0

    upload_result = screenshot_result_upload(
        result,
        screenshot_root=mailbox.root / "screenshots",
        config_path=args.config,
    )
    if isinstance(operation_result, dict):
        operation_result["drive_upload"] = upload_result
        try:
            mailbox.update_result(result)
        except Exception:
            upload_result = dict(upload_result)
            upload_result["result_recorded"] = False
            upload_result["record_error"] = {
                "code": "result_update_failed",
                "message": "Upload status could not be written to the local mailbox result.",
            }
    _print(upload_result)
    return 0 if upload_result.get("status") == "uploaded" and upload_result.get("result_recorded", True) else 1
