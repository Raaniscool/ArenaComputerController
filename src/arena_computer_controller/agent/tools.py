"""Agent tools adapted from the existing local MCP bridge."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from ..integrations.mcp_bridge import ControllerMCPBridge, ToolOutcome
from .types import ImageAttachment, ToolResult, ToolSpec


_BLENDER_OPERATIONS = [
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
]


def _error_result(code: str, message: str) -> ToolResult:
    return ToolResult({"ok": False, "error": {"code": code, "message": message}}, is_error=True)


def _from_outcome(outcome: ToolOutcome) -> ToolResult:
    if (outcome.image is None) != (outcome.image_mime_type is None):
        return _error_result("invalid_image_result", "The local bridge returned incomplete image metadata.")
    image = None
    if outcome.image is not None:
        image = ImageAttachment(mime_type=outcome.image_mime_type, data=outcome.image)
    return ToolResult(data=outcome.data, image=image, is_error=outcome.is_error)


def _has_only(arguments: Mapping[str, Any], allowed: set[str]) -> bool:
    return all(isinstance(key, str) and key in allowed for key in arguments)


def _agent_request_id(call_id: str) -> str:
    """Make a stable, local request id for retries of the same model tool call."""
    digest = hashlib.sha256(call_id.encode("utf-8", errors="replace")).hexdigest()[:40]
    return "agent-" + digest


def build_controller_tools(bridge: ControllerMCPBridge) -> tuple[ToolSpec, ...]:
    """Create tool definitions backed by the same bridge as the stdio MCP server.

    The current six-tool surface is retained. Mutating Blender operations are
    gated by the agent runtime's human-approval callback.
    """

    def get_computer_state(arguments: dict[str, Any], _call_id: str) -> ToolResult:
        if arguments:
            return _error_result("invalid_arguments", "get_computer_state accepts no arguments.")
        return _from_outcome(bridge.get_computer_state())

    def get_active_window(arguments: dict[str, Any], _call_id: str) -> ToolResult:
        if arguments:
            return _error_result("invalid_arguments", "get_active_window accepts no arguments.")
        return _from_outcome(bridge.get_active_window())

    def get_screenshot(arguments: dict[str, Any], _call_id: str) -> ToolResult:
        allowed = {"format", "quality", "scale", "force", "window_id"}
        if not _has_only(arguments, allowed):
            return _error_result("invalid_arguments", "get_screenshot received an unsupported argument.")
        return _from_outcome(
            bridge.get_screenshot(
                format=arguments.get("format", "jpeg"),
                quality=arguments.get("quality", 90),
                scale=arguments.get("scale", 0.75),
                force=arguments.get("force", False),
                window_id=arguments.get("window_id"),
            )
        )

    def blender_probe(arguments: dict[str, Any], _call_id: str) -> ToolResult:
        if not _has_only(arguments, {"timeout"}):
            return _error_result("invalid_arguments", "blender_probe received an unsupported argument.")
        return _from_outcome(bridge.blender_probe(timeout=arguments.get("timeout", 5.0)))

    def blender_execute(arguments: dict[str, Any], call_id: str) -> ToolResult:
        allowed = {"operation", "parameters", "timeout", "request_id"}
        if not _has_only(arguments, allowed):
            return _error_result("invalid_arguments", "blender_execute received an unsupported argument.")
        operation = arguments.get("operation")
        parameters = arguments.get("parameters", {})
        request_id = arguments.get("request_id")
        if request_id is None:
            if not isinstance(call_id, str) or not call_id:
                return _error_result("missing_tool_call_id", "A tool-call ID is required to create a stable request ID.")
            request_id = _agent_request_id(call_id)
        return _from_outcome(
            bridge.blender_execute(
                operation=operation,
                parameters=parameters,
                timeout=arguments.get("timeout", 60.0),
                request_id=request_id,
            )
        )

    def blender_get_screenshot(arguments: dict[str, Any], _call_id: str) -> ToolResult:
        if not _has_only(arguments, {"job_id", "force"}):
            return _error_result("invalid_arguments", "blender_get_screenshot received an unsupported argument.")
        if "job_id" not in arguments:
            return _error_result("invalid_arguments", "blender_get_screenshot requires job_id.")
        return _from_outcome(
            bridge.blender_get_screenshot(
                job_id=arguments["job_id"],
                force=arguments.get("force", False),
            )
        )

    return (
        ToolSpec(
            name="get_computer_state",
            description="Read detected applications, windows, and controller state. Does not change the desktop.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=get_computer_state,
        ),
        ToolSpec(
            name="get_active_window",
            description="Return the currently active window record, or null if none is detected.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=get_active_window,
        ),
        ToolSpec(
            name="get_screenshot",
            description=(
                "Capture the current desktop on request and return a native image for visual inspection. "
                "Defaults to JPEG quality 90 at 0.75 scale; PNG is available for lossless pixels. "
                "Identical repeats are omitted unless force=true. Optionally pass a visible window_id "
                "returned by get_computer_state or get_active_window."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["jpeg", "jpg", "png", "webp"]},
                    "quality": {"type": "integer", "minimum": 1, "maximum": 100},
                    "scale": {"type": "number", "minimum": 0.1, "maximum": 1.0},
                    "force": {"type": "boolean"},
                    "window_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            execute=get_screenshot,
        ),
        ToolSpec(
            name="blender_probe",
            description="Check Blender add-on responsiveness with the harmless hello_test job.",
            parameters={
                "type": "object",
                "properties": {"timeout": {"type": "number", "minimum": 0.1, "maximum": 540}},
                "additionalProperties": False,
            },
            execute=blender_probe,
        ),
        ToolSpec(
            name="blender_execute",
            description=(
                "Execute one operation from the existing Blender allowlist through the local controller. "
                "Every change requires explicit user approval in this application. Never invent object state; "
                "inspect the tool result or returned render. Supported parameter shapes: hello_test {message}; "
                "create_test_object {}; create_primitive {primitive_type, object_name, optional location/rotation/scale}; "
                "move_object {object_name, location}; rotate_object {object_name, rotation}; "
                "scale_object {object_name, scale}; rename_object {object_name, new_name}; "
                "create_material {material_name, object_name, base_color RGBA, roughness, metallic}; "
                "capture_screenshot {optional width, height}; duplicate_object {object_name, new_name, optional location}; "
                "delete_object {object_name}; set_dimensions {object_name, dimensions}. Vectors are three numbers "
                "except base_color (four); primitive_type must be cube, uv_sphere, cylinder, plane, or cone. "
                "request_id is optional; the runtime supplies a stable id for this model tool call."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": _BLENDER_OPERATIONS},
                    "parameters": {"type": "object"},
                    "timeout": {"type": "number", "minimum": 0.1, "maximum": 540},
                    "request_id": {"type": "string", "minLength": 1, "maxLength": 512},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
            execute=blender_execute,
            requires_approval=True,
        ),
        ToolSpec(
            name="blender_get_screenshot",
            description=(
                "Fetch a completed Blender camera-render PNG by job_id and return the actual image "
                "for visual inspection. Identical image bytes are omitted unless force=true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "force": {"type": "boolean"},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
            execute=blender_get_screenshot,
        ),
    )


class ToolRegistry:
    """Ordered registry that rejects duplicate tool names."""

    def __init__(self, tools: Sequence[ToolSpec]):
        by_name: dict[str, ToolSpec] = {}
        for tool in tools:
            if not tool.name or tool.name in by_name:
                raise ValueError(f"Tool names must be non-empty and unique: {tool.name!r}.")
            by_name[tool.name] = tool
        self._tools = by_name

    @property
    def tools(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)
