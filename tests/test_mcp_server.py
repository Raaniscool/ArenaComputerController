import asyncio
import base64
import importlib.util
import os
import sys
import unittest
from pathlib import Path

from arena_computer_controller.integrations.mcp_bridge import ToolOutcome
from arena_workshop.protocol import SUPPORTED_OPERATIONS


HAS_MCP = importlib.util.find_spec("mcp") is not None
ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"native-image-block"
JPEG = b"\xff\xd8\xffnative-jpeg-block"


class FakeBridge:
    def get_computer_state(self):
        return ToolOutcome({"ok": True, "active_window": {"title": "Blender"}})

    def get_active_window(self):
        return ToolOutcome({"ok": True, "active_window": {"title": "Blender"}})

    def get_screenshot(self, format="jpeg", quality=90, scale=0.75, force=False, window_id=None):
        return ToolOutcome({"ok": True, "image_included": True}, image=JPEG, image_mime_type="image/jpeg")

    def blender_probe(self, timeout=5.0):
        return ToolOutcome({"healthy": True, "job_id": "probe-1"})

    def blender_execute(self, operation, parameters=None, timeout=60.0, request_id=None):
        return ToolOutcome({"ok": True, "request_id": request_id})

    def blender_get_screenshot(self, job_id, force=False):
        return ToolOutcome(
            {"ok": True, "job_id": job_id, "image_included": True},
            image=PNG,
            image_mime_type="image/png",
        )


@unittest.skipUnless(HAS_MCP, "install the optional MCP extra to exercise MCP SDK integration")
class MCPServerTests(unittest.TestCase):
    def setUp(self):
        from arena_computer_controller.mcp_server import create_mcp_server

        self.server = create_mcp_server(FakeBridge())

    def test_registers_only_six_bounded_controller_tools(self):
        tools = asyncio.run(self.server.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(names, {
            "get_computer_state",
            "get_active_window",
            "get_screenshot",
            "blender_probe",
            "blender_execute",
            "blender_get_screenshot",
        })
        self.assertNotIn("execute_python", names)
        self.assertNotIn("run_shell", names)
        execute = next(tool for tool in tools if tool.name == "blender_execute")
        self.assertTrue(execute.annotations.destructive_hint)
        self.assertIn("request_id", execute.input_schema["required"])
        operation_values = execute.input_schema["properties"]["operation"]["enum"]
        self.assertEqual(tuple(operation_values), SUPPORTED_OPERATIONS)
        self.assertIn("existing Blender allowlist", execute.description)

    def test_blender_execute_forwards_required_stable_request_id(self):
        result = asyncio.run(self.server.call_tool("blender_execute", {
            "operation": "create_test_object",
            "request_id": "stable-123",
            "parameters": {},
        }))
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["request_id"], "stable-123")

    def test_desktop_jpeg_tool_result_keeps_native_jpeg_mime_type(self):
        result = asyncio.run(self.server.call_tool("get_screenshot", {}))
        self.assertEqual(result.content[1].type, "image")
        self.assertEqual(result.content[1].mime_type, "image/jpeg")
        self.assertEqual(base64.b64decode(result.content[1].data), JPEG)

    def test_image_tool_result_has_structured_json_and_native_image(self):
        result = asyncio.run(self.server.call_tool("blender_get_screenshot", {"job_id": "job-7"}))
        self.assertEqual(result.structured_content["job_id"], "job-7")
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.content), 2)
        self.assertEqual(result.content[0].type, "text")
        self.assertEqual(result.content[1].type, "image")
        self.assertEqual(result.content[1].mime_type, "image/png")
        self.assertEqual(base64.b64decode(result.content[1].data), PNG)

    def test_stdio_client_transfers_native_image_result(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def round_trip():
            env = os.environ.copy()
            prior_pythonpath = env.get("PYTHONPATH")
            paths = [str(ROOT / "src"), str(ROOT)]
            if prior_pythonpath:
                paths.append(prior_pythonpath)
            env["PYTHONPATH"] = os.pathsep.join(paths)
            env["ARENA_CONTROLLER_URL"] = "http://127.0.0.1:8765"
            env.pop("ARENA_CONTROLLER_TOKEN", None)
            script = (
                "from arena_computer_controller.mcp_server import create_mcp_server; "
                "from tests.test_mcp_server import FakeBridge; "
                "create_mcp_server(FakeBridge()).run(transport='stdio')"
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=["-c", script],
                env=env,
                cwd=ROOT,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool("blender_get_screenshot", {"job_id": "job-7"})
                    image = next(content for content in result.content if content.type == "image")
                    return image.mime_type, base64.b64decode(image.data)

        mime_type, image_data = asyncio.run(round_trip())
        self.assertEqual(mime_type, "image/png")
        self.assertEqual(image_data, PNG)

    def test_stdio_client_initializes_and_lists_tools(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def round_trip():
            env = os.environ.copy()
            prior_pythonpath = env.get("PYTHONPATH")
            paths = [str(ROOT / "src"), str(ROOT)]
            if prior_pythonpath:
                paths.append(prior_pythonpath)
            env["PYTHONPATH"] = os.pathsep.join(paths)
            env["ARENA_CONTROLLER_URL"] = "http://127.0.0.1:8765"
            env.pop("ARENA_CONTROLLER_TOKEN", None)
            env["ARENA_MCP_MAX_IMAGE_BYTES"] = str(8 * 1024 * 1024)
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "arena_computer_controller.mcp_server"],
                env=env,
                cwd=ROOT,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return {tool.name for tool in result.tools}

        names = asyncio.run(round_trip())
        self.assertEqual(len(names), 6)
        self.assertIn("blender_execute", names)
        self.assertIn("blender_get_screenshot", names)


if __name__ == "__main__":
    unittest.main()
