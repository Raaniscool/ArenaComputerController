"""Strict, dependency-free job protocol shared by the worker and Blender add-on."""

import json
import math
import re
from datetime import datetime, timedelta, timezone

PROTOCOL_VERSION = "1.0"
MAX_JOB_BYTES = 16 * 1024
MAX_JOB_AGE = timedelta(hours=1)
MAX_JOB_LIFETIME = timedelta(minutes=30)
MAX_CLOCK_SKEW = timedelta(minutes=5)
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUIRED_FIELDS = {
    "protocol_version",
    "job_id",
    "workspace_id",
    "operation",
    "parameters",
    "status",
    "created_at",
    "deadline",
    "result",
    "error",
}
SUPPORTED_OPERATIONS = (
    "hello_test",
    "create_test_object",
    "create_primitive",
    "move_object",
    "rotate_object",
    "scale_object",
    "rename_object",
    "create_material",
    "capture_screenshot",
    "duplicate_object",
    "delete_object",
    "set_dimensions",
)
PRIMITIVE_TYPES = {"cube", "uv_sphere", "cylinder", "plane", "cone"}
MAX_OBJECT_NAME_LENGTH = 63


class JobValidationError(ValueError):
    """A job violates the protocol or local allowlist."""


def _reject_constant(value):
    raise JobValidationError("non-finite_json_number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JobValidationError("duplicate_json_key")
        result[key] = value
    return result


def _parse_utc(value, field):
    if not isinstance(value, str) or len(value) > 40:
        raise JobValidationError("invalid_" + field)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError):
        raise JobValidationError("invalid_" + field)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise JobValidationError("timestamp_must_be_utc")
    return parsed.astimezone(timezone.utc)


def _validate_name(value, field):
    if not isinstance(value, str) or not value or value != value.strip():
        raise JobValidationError("invalid_" + field)
    if len(value) > MAX_OBJECT_NAME_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise JobValidationError("invalid_" + field)


def _validate_vector(value, field, size=3, minimum=None, maximum=None, nonzero=False):
    if not isinstance(value, list) or len(value) != size:
        raise JobValidationError("invalid_" + field)
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise JobValidationError("invalid_" + field)
        try:
            number = float(component)
        except (OverflowError, ValueError):
            raise JobValidationError("invalid_" + field)
        if not math.isfinite(number):
            raise JobValidationError("invalid_" + field)
        if minimum is not None and number < minimum:
            raise JobValidationError("invalid_" + field)
        if maximum is not None and number > maximum:
            raise JobValidationError("invalid_" + field)
        if nonzero and number == 0:
            raise JobValidationError("invalid_" + field)


def _validate_params(operation, params):
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS:
        raise JobValidationError("unsupported_operation")
    if operation == "hello_test":
        if set(params) != {"message"}:
            raise JobValidationError("invalid_hello_test_parameters")
        message = params["message"]
        if not isinstance(message, str) or not message.strip() or len(message) > 256:
            raise JobValidationError("invalid_hello_test_message")
        return

    if operation == "create_test_object":
        if params:
            raise JobValidationError("create_test_object_takes_no_parameters")
        return

    if operation == "create_primitive":
        required = {"primitive_type", "object_name"}
        allowed = required | {"location", "rotation", "scale"}
        if not required.issubset(params) or not set(params).issubset(allowed):
            raise JobValidationError("invalid_create_primitive_parameters")
        if not isinstance(params["primitive_type"], str) or params["primitive_type"] not in PRIMITIVE_TYPES:
            raise JobValidationError("invalid_primitive_type")
        _validate_name(params["object_name"], "object_name")
        if "location" in params:
            _validate_vector(params["location"], "location", minimum=-1_000_000, maximum=1_000_000)
        if "rotation" in params:
            _validate_vector(params["rotation"], "rotation", minimum=-360_000, maximum=360_000)
        if "scale" in params:
            _validate_vector(params["scale"], "scale", minimum=-10_000, maximum=10_000, nonzero=True)
        return

    vector_operations = {
        "move_object": ("location", -1_000_000, 1_000_000, False),
        "rotate_object": ("rotation", -360_000, 360_000, False),
        "scale_object": ("scale", -10_000, 10_000, True),
    }
    if operation in vector_operations:
        field, minimum, maximum, nonzero = vector_operations[operation]
        if set(params) != {"object_name", field}:
            raise JobValidationError("invalid_" + operation + "_parameters")
        _validate_name(params["object_name"], "object_name")
        _validate_vector(params[field], field, minimum=minimum, maximum=maximum, nonzero=nonzero)
        return

    if operation == "rename_object":
        if set(params) != {"object_name", "new_name"}:
            raise JobValidationError("invalid_rename_object_parameters")
        _validate_name(params["object_name"], "object_name")
        _validate_name(params["new_name"], "new_name")
        return

    if operation == "create_material":
        expected = {"material_name", "object_name", "base_color", "roughness", "metallic"}
        if set(params) != expected:
            raise JobValidationError("invalid_create_material_parameters")
        _validate_name(params["material_name"], "material_name")
        _validate_name(params["object_name"], "object_name")
        _validate_vector(params["base_color"], "base_color", size=4, minimum=0, maximum=1)
        for field in ("roughness", "metallic"):
            _validate_vector([params[field]], field, size=1, minimum=0, maximum=1)
        return

    if operation == "capture_screenshot":
        if not set(params).issubset({"width", "height"}):
            raise JobValidationError("invalid_capture_screenshot_parameters")
        for field, maximum in (("width", 1280), ("height", 720)):
            value = params.get(field, 640 if field == "width" else 480)
            if isinstance(value, bool) or not isinstance(value, int) or not 64 <= value <= maximum:
                raise JobValidationError("invalid_screenshot_" + field)
        return

    if operation == "duplicate_object":
        if set(params) not in ({"object_name", "new_name"}, {"object_name", "new_name", "location"}):
            raise JobValidationError("invalid_duplicate_object_parameters")
        _validate_name(params["object_name"], "object_name")
        _validate_name(params["new_name"], "new_name")
        if params["object_name"] == params["new_name"]:
            raise JobValidationError("duplicate_name_must_differ")
        if "location" in params:
            _validate_vector(params["location"], "location", minimum=-1_000_000, maximum=1_000_000)
        return

    if operation == "delete_object":
        if set(params) != {"object_name"}:
            raise JobValidationError("invalid_delete_object_parameters")
        _validate_name(params["object_name"], "object_name")
        return

    if operation == "set_dimensions":
        if set(params) != {"object_name", "dimensions"}:
            raise JobValidationError("invalid_set_dimensions_parameters")
        _validate_name(params["object_name"], "object_name")
        _validate_vector(params["dimensions"], "dimensions", minimum=0.001, maximum=100_000)
        return

    raise JobValidationError("unsupported_operation")


def parse_job_description(description):
    """Parse one raw Linear issue description without normalizing its JSON text."""
    if not isinstance(description, str) or not description:
        raise JobValidationError("description_must_be_text")
    try:
        encoded = description.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JobValidationError("invalid_utf8") from exc
    if len(encoded) > MAX_JOB_BYTES:
        raise JobValidationError("job_too_large")
    try:
        request = json.loads(
            description,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except JobValidationError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise JobValidationError("invalid_json") from exc
    if not isinstance(request, dict) or set(request) != {"operation", "parameters"}:
        raise JobValidationError("invalid_job_fields")
    if not isinstance(request["parameters"], dict):
        raise JobValidationError("parameters_must_be_object")
    return request


def validate_job_description(description):
    """Parse and validate a Linear request with the same operation schema as the worker."""
    request = parse_job_description(description)
    _validate_params(request["operation"], request["parameters"])
    return request


def decode_job(raw, allowed_workspaces, now=None):
    """Decode and validate an incoming job. Only queued MVP operations are accepted."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_JOB_BYTES:
            raise JobValidationError("job_too_large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise JobValidationError("invalid_utf8")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_JOB_BYTES:
        raise JobValidationError("job_too_large_or_not_text")
    try:
        job = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except JobValidationError:
        raise
    except (json.JSONDecodeError, TypeError):
        raise JobValidationError("invalid_json")
    return validate_job(job, allowed_workspaces=allowed_workspaces, now=now)


def validate_job(job, allowed_workspaces, now=None):
    if not isinstance(job, dict) or set(job) != _REQUIRED_FIELDS:
        raise JobValidationError("invalid_job_fields")
    if job["protocol_version"] != PROTOCOL_VERSION:
        raise JobValidationError("unsupported_protocol_version")

    job_id = job["job_id"]
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise JobValidationError("invalid_job_id")

    workspace_id = job["workspace_id"]
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID.fullmatch(workspace_id):
        raise JobValidationError("invalid_workspace_id")
    if workspace_id not in set(allowed_workspaces):
        raise JobValidationError("workspace_not_allowed")

    if job["status"] != "queued" or job["result"] is not None or job["error"] is not None:
        raise JobValidationError("invalid_initial_state")

    created_at = _parse_utc(job["created_at"], "created_at")
    deadline = _parse_utc(job["deadline"], "deadline")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if created_at > now + MAX_CLOCK_SKEW:
        raise JobValidationError("created_at_in_future")
    if created_at < now - MAX_JOB_AGE:
        raise JobValidationError("job_too_old")
    if deadline <= now:
        raise JobValidationError("job_expired")
    if deadline <= created_at or deadline - created_at > MAX_JOB_LIFETIME:
        raise JobValidationError("invalid_deadline")

    operation = job["operation"]
    params = job["parameters"]
    if not isinstance(params, dict):
        raise JobValidationError("parameters_must_be_object")
    _validate_params(operation, params)
    return dict(job)
