"""Local setup and runner commands for the outbound-only Linear worker."""

import argparse
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path

from .linear_api import LinearApi, LinearApiError, LinearCredentialStore
from .linear_worker import QUEUE_LABEL, TEAM_KEY, LinearWorker, WindowsMutex, WorkerError
from .protocol import JobValidationError, MAX_JOB_BYTES, validate_job_description


def configure_credentials(client_id, client_secret, store=None, api_factory=LinearApi):
    """Validate OAuth credentials, discard the test token, and DPAPI-store secrets."""
    if not isinstance(client_id, str) or not client_id.strip():
        raise LinearApiError("invalid_credentials", "A Linear OAuth client ID is required.")
    if not isinstance(client_secret, str) or not client_secret:
        raise LinearApiError("invalid_credentials", "A Linear OAuth client secret is required.")
    api = api_factory(client_id.strip(), client_secret)
    api.authenticate_and_forget()
    (store or LinearCredentialStore()).save(client_id.strip(), client_secret)
    return True


def configure_main(argv=None, input_fn=input, secret_prompt=getpass.getpass, store=None, api_factory=LinearApi):
    parser = argparse.ArgumentParser(
        prog="python -m arena_workshop configure-linear",
        description="Validate and DPAPI-protect the Linear worker OAuth client credentials on this Windows user profile.",
    )
    parser.parse_args(argv)
    if os.name != "nt":
        print("Linear worker credentials require Windows DPAPI; no plaintext fallback is available.", file=sys.stderr)
        return 2
    client_secret = None
    try:
        client_id = input_fn("Linear OAuth client ID: ").strip()
        client_secret = secret_prompt("Linear OAuth client secret (input hidden): ")
        configure_credentials(client_id, client_secret, store=store, api_factory=api_factory)
    except (LinearApiError, OSError) as exc:
        code = getattr(exc, "code", "credential_setup_failed")
        print("Linear credential setup failed ({}): {}".format(code, getattr(exc, "message", "local storage failed")), file=sys.stderr)
        return 1
    finally:
        client_secret = None
    print("Linear credentials validated and stored using Windows DPAPI for this Windows user.")
    print("The access token was discarded; the worker requests a fresh token when it starts.")
    return 0


def _read_job_description(path, input_stream=None):
    """Read raw UTF-8 description bytes without shell/JSON re-serialization."""
    try:
        if path == "-":
            stream = input_stream if input_stream is not None else sys.stdin.buffer
            raw = stream.read(MAX_JOB_BYTES + 1)
        else:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_JOB_BYTES + 1)
    except OSError as exc:
        raise JobValidationError("description_read_failed") from exc
    if len(raw) > MAX_JOB_BYTES:
        raise JobValidationError("job_too_large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JobValidationError("invalid_utf8") from exc


def submit_main(argv=None, store=None, api_factory=LinearApi, input_stream=None):
    """Submit one validated job into Backlog, verify it, then optionally dispatch."""
    parser = argparse.ArgumentParser(
        prog="python -m arena_workshop linear-submit",
        description=("Create a BlenderArena job from a UTF-8 JSON file, verify the exact "
                     "Linear round trip, and optionally move it to Todo."),
    )
    parser.add_argument("--title", required=True, help="Linear issue title")
    parser.add_argument("--description-file", required=True,
                        help="raw UTF-8 job JSON file, or - to read stdin")
    parser.add_argument("--dispatch", action="store_true",
                        help="move the verified new issue from Backlog to Todo")
    args = parser.parse_args(argv)

    try:
        description = _read_job_description(args.description_file, input_stream=input_stream)
        request = validate_job_description(description)
    except JobValidationError as exc:
        print("Linear job description rejected ({}).".format(str(exc)), file=sys.stderr)
        return 2

    phase = "load protected credentials"
    try:
        credentials = (store or LinearCredentialStore()).load()
        if credentials is None:
            raise LinearApiError("authorization_required", "Run configure-linear on this Windows PC first.")
        api = api_factory(credentials["client_id"], credentials["client_secret"])
        dispatch_attempted = False
        phase = "resolve team BLE"
        team_id = api.get_team_id(TEAM_KEY)
        phase = "resolve workflow states"
        states = api.get_workflow_states(TEAM_KEY)
        backlog_id = states.get("Backlog")
        todo_id = states.get("Todo")
        if not isinstance(backlog_id, str) or not backlog_id:
            raise LinearApiError("workflow_state_missing", "BlenderArena is missing the Backlog workflow state.")
        phase = "resolve team-scoped job label"
        label_id = api.get_issue_label_id(team_id, QUEUE_LABEL)
        phase = "create issue in Backlog"
        created = api.create_issue(
            team_id=team_id,
            title=args.title,
            description=description,
            state_id=backlog_id,
            label_ids=[label_id],
            priority=3,
        )
        identifier = created.get("identifier") if isinstance(created, dict) else None
        issue_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(identifier, str) or not isinstance(issue_id, str):
            raise LinearApiError("invalid_linear_response", "Linear created an issue without a stable identifier.")

        phase = "verify created issue round trip"
        verified = api.get_issue(identifier)
        if not isinstance(verified, dict):
            raise LinearApiError("invalid_linear_response", "Linear did not return the newly created job issue.")
        if (verified.get("description") != description
                or validate_job_description(verified.get("description")) != request):
            raise LinearApiError(
                "issue_description_roundtrip_failed",
                "Linear did not preserve the submitted JSON description exactly; the new issue remains in Backlog.",
            )
        current_state = verified.get("state")
        if not isinstance(current_state, dict) or current_state.get("name") != "Backlog":
            raise LinearApiError(
                "issue_state_unexpected",
                "The new issue was not verified in Backlog; it was not dispatched.",
            )
        team = verified.get("team")
        labels = verified.get("labels")
        label_nodes = labels.get("nodes") if isinstance(labels, dict) else None
        if (not isinstance(team, dict) or team.get("key") != TEAM_KEY
                or not isinstance(label_nodes, list)
                or not any(isinstance(label, dict) and label.get("name") == QUEUE_LABEL for label in label_nodes)):
            raise LinearApiError(
                "issue_target_verification_failed",
                "The new issue's team or job label could not be verified; it was not dispatched.",
            )

        state_name = "Backlog"
        if args.dispatch:
            if not isinstance(todo_id, str) or not todo_id:
                raise LinearApiError("workflow_state_missing", "BlenderArena is missing the Todo workflow state.")
            phase = "dispatch verified issue to Todo"
            dispatch_attempted = True
            api.update_issue_state(issue_id, todo_id)
            phase = "verify dispatched issue"
            verified = api.get_issue(identifier)
            if not isinstance(verified, dict):
                raise LinearApiError("invalid_linear_response", "Linear did not return the dispatched job issue.")
            current_state = verified.get("state")
            if (not isinstance(current_state, dict) or current_state.get("name") != "Todo"
                    or verified.get("description") != description):
                raise LinearApiError(
                    "issue_dispatch_verification_failed",
                    "The new issue's Todo state or JSON description could not be verified.",
                )
            state_name = "Todo"
    except (LinearApiError, OSError, JobValidationError) as exc:
        code = getattr(exc, "code", str(exc) if isinstance(exc, JobValidationError) else "linear_submit_failed")
        message = getattr(exc, "message", "The job issue could not be submitted safely.")
        status = getattr(exc, "status", None)
        endpoint = getattr(exc, "endpoint", None)
        endpoint_text = " via {}".format(endpoint) if endpoint else ""
        status_text = ", HTTP {}".format(status) if status is not None else ""
        print("Linear job submission failed during {}{} ({}{}): {}".format(
            phase, endpoint_text, code, status_text, message,
        ), file=sys.stderr)
        if "identifier" in locals() and isinstance(identifier, str):
            if locals().get("dispatch_attempted"):
                detail = "dispatch may have been applied; inspect the new issue before retrying."
            else:
                detail = "the new issue was not dispatched."
            print("Created issue {}; {}".format(identifier, detail), file=sys.stderr)
        return 1

    print(json.dumps({
        "identifier": identifier,
        "state": state_name,
        "description_verified": True,
        "operation": request["operation"],
    }, sort_keys=True))
    return 0


def worker_main(argv=None, store=None, api_factory=LinearApi, worker_factory=LinearWorker, mutex_factory=WindowsMutex):
    parser = argparse.ArgumentParser(
        prog="python -m arena_workshop linear-worker",
        description="Run the outbound-only, single-instance Linear-to-Blender mailbox worker.",
    )
    parser.add_argument("--once", action="store_true", help="perform one reconciliation cycle and exit")
    parser.add_argument("--poll-interval", type=float, default=5.0, metavar="SECONDS",
                        help="poll delay (1-60 seconds, default 5)")
    args = parser.parse_args(argv)
    if not 1.0 <= args.poll_interval <= 60.0:
        parser.error("--poll-interval must be between 1 and 60 seconds")
    if os.name != "nt":
        print("The Linear worker requires Windows and a Windows named mutex.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        with mutex_factory():
            credential_store = store or LinearCredentialStore()
            credentials = credential_store.load()
            if credentials is None:
                raise LinearApiError("authorization_required", "Run configure-linear on this Windows PC first.")
            api = api_factory(credentials["client_id"], credentials["client_secret"])
            worker = worker_factory(api)
            if args.once:
                worker.initialize()
                worker.run_once()
            else:
                worker.run_forever(poll_interval=args.poll_interval, sleep=time.sleep)
    except (LinearApiError, WorkerError, OSError) as exc:
        code = getattr(exc, "code", "worker_start_failed")
        message = getattr(exc, "message", "The Linear worker could not continue.")
        print("Linear worker stopped ({}): {}".format(code, message), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0
