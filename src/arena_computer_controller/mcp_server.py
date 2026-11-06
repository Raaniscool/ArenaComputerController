"""Expose the existing local controller as a small stdio-only MCP server.

The MCP server never controls Blender directly. It calls the controller's existing
loopback HTTP API, which in turn uses the already-configured Blender mailbox adapter.
It intentionally creates no network listener of its own.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Literal

from .integrations.mcp_bridge import ControllerHTTPClient, ControllerMCPBridge, ToolOutcome


_TOOL_INSTRUCTIONS = """Use the computer and Blender tools only for the user's requested task. Blender execution is limited to the controller's existing allowlisted operations; arbitrary Python, shell commands, and unlisted operations are unavailable. Preserve request_id when retrying a Blender command. A successful render can be retrieved with blender_get_screenshot; the tool returns a native image block when the PNG is new. If image_included is false, the bytes match the prior image from this MCP process; set force=true if you need the image payload again. The controller does not provide a complete scene inventory, so do not infer object properties that were not returned by a tool."""

BlenderOperation = Literal[
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


def _to_mcp_result(outcome: ToolOutcome) -> Any:
    """Convert structured tool data and optional bytes to MCP content blocks."""
    from mcp.server.mcpserver.utilities.types import Image
    from mcp.types import CallToolResult, TextContent

    blocks: list[Any] = [TextContent(text=json.dumps(outcome.data, ensure_ascii=False, separators=(",", ":")))]
    if outcome.image is not None and outcome.image_mime_type is not None:
        image_format = outcome.image_mime_type.split("/", 1)[1]
        blocks.append(Image(data=outcome.image, format=image_format).to_image_content())
    return CallToolResult(content=blocks, structuredContent=outcome.data, isError=outcome.is_error)


def create_mcp_server(bridge: ControllerMCPBridge | None = None) -> Any:
    """Create the six-tool MCP server. Imports the optional SDK only when called."""
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError("The MCP extra is not installed. Install with: pip install -e '.[mcp]'.") from exc

    bridge = bridge or ControllerMCPBridge(ControllerHTTPClient(
        base_url=os.environ.get("ARENA_CONTROLLER_URL", "http://127.0.0.1:8765"),
        token=os.environ.get("ARENA_CONTROLLER_TOKEN"),
        max_image_bytes=_max_image_bytes_from_environment(),
    ))
    server = MCPServer(
        name="ArenaComputerController",
        title="Arena Computer Controller",
        instructions=_TOOL_INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool(
        name="get_computer_state",
        description="Read detected applications, windows, and controller state. Does not change the desktop.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    def get_computer_state():
        return _to_mcp_result(bridge.get_computer_state())

    @server.tool(
        name="get_active_window",
        description="Return the currently active window record, or null if none is detected.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    def get_active_window():
        return _to_mcp_result(bridge.get_active_window())

    @server.tool(
        name="get_screenshot",
        description="Capture the current desktop on request and return it as a native MCP image block. Optionally pass a window_id returned by get_computer_state or get_active_window to capture that window's visible bounds. Defaults to a size-conscious JPEG; choose PNG for lossless pixels. Repeated identical images are not resent unless force=true.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    def get_screenshot(
        format: str = "jpeg",
        quality: int = 90,
        scale: float = 0.75,
        force: bool = False,
        window_id: str | None = None,
    ):
        return _to_mcp_result(bridge.get_screenshot(format, quality, scale, force, window_id))

    @server.tool(
        name="blender_probe",
        description="Check Blender add-on responsiveness with the harmless hello_test job.",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=False),
        structured_output=False,
    )
    def blender_probe(timeout: float = 5.0):
        return _to_mcp_result(bridge.blender_probe(timeout))

    @server.tool(
        name="blender_execute",
        description=("Execute one operation from the existing Blender allowlist through the local controller; the controller and connector validate every field. Supported operation names are hello_test, create_test_object, create_primitive, move_object, rotate_object, scale_object, rename_object, create_material, capture_screenshot, duplicate_object, delete_object, and set_dimensions. Existing parameter shapes: hello_test {message}; create_test_object {}; create_primitive {primitive_type, object_name, optional location/rotation/scale}; move_object {object_name, location}; rotate_object {object_name, rotation}; scale_object {object_name, scale}; rename_object {object_name, new_name}; create_material {material_name, object_name, base_color RGBA, roughness, metallic}; capture_screenshot {optional width, height}; duplicate_object {object_name, new_name, optional location}; delete_object {object_name}; set_dimensions {object_name, dimensions}. Vectors are three numbers except base_color (four); use primitive types cube, uv_sphere, cylinder, plane, cone. request_id is required: provide a unique value and reuse it unchanged if retrying the same operation. This is not an inventory API; inspect the returned results or image, and do not assume unreported scene state."),
        annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False, openWorldHint=False),
        structured_output=False,
    )
    def blender_execute(
        operation: BlenderOperation,
        request_id: str,
        parameters: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ):
        return _to_mcp_result(bridge.blender_execute(operation, parameters, timeout, request_id))

    @server.tool(
        name="blender_get_screenshot",
        description="Fetch a completed Blender camera-render PNG by job_id. The result contains metadata and, for a new image, a native MCP image block. Repeated identical image bytes are omitted unless force=true.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    def blender_get_screenshot(job_id: str, force: bool = False):
        return _to_mcp_result(bridge.blender_get_screenshot(job_id, force))

    return server


def _max_image_bytes_from_environment() -> int:
    raw = os.environ.get("ARENA_MCP_MAX_IMAGE_BYTES", str(8 * 1024 * 1024))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("ARENA_MCP_MAX_IMAGE_BYTES must be an integer.") from None
    if not 1024 <= value <= 20 * 1024 * 1024:
        raise ValueError("ARENA_MCP_MAX_IMAGE_BYTES must be between 1 KiB and 20 MiB.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a stdio-only MCP adapter over the local Arena computer controller."
    )
    parser.parse_args(argv)
    try:
        server = create_mcp_server()
        server.run(transport="stdio")
    except (ImportError, RuntimeError, ValueError) as exc:
        print(f"Arena MCP server stopped: {exc}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
