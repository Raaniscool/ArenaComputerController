"""Command-line entry point for the independent local agent runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..integrations.mcp_bridge import ControllerHTTPClient, ControllerMCPBridge
from .openai_compatible import ModelProviderError, OpenAICompatibleProvider
from .runtime import AgentRuntime
from .tools import build_controller_tools
from .types import AgentEvent, ToolCall, ToolSpec

_DEFAULT_BASE_URL = "https://api.arena.ai/v1"
_DEFAULT_MODEL = "coding-router-preview"


def _integer_option(name: str, minimum: int):
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"{name} must be an integer.") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"{name} must be at least {minimum}.")
        return value

    return parse


def _progress(event: AgentEvent) -> None:
    if event.kind == "model_request":
        print(f"[agent] {event.message}", file=sys.stderr, flush=True)
    elif event.kind == "tool_started":
        print(f"[tool] {event.tool_name}: started", file=sys.stderr, flush=True)
    elif event.kind == "tool_completed":
        image = "; image attached for the next model request" if event.data.get("image_included") else ""
        error = "; tool reported an error" if event.data.get("is_error") else ""
        print(f"[tool] {event.tool_name}: completed{image}{error}", file=sys.stderr, flush=True)
    elif event.kind in {"approval_denied", "tool_limit", "turn_limit"}:
        print(f"[agent] {event.message}", file=sys.stderr, flush=True)


def _approve_blender_mutation(tool: ToolSpec, call: ToolCall) -> bool:
    print("\nApproval required: this Blender tool can change the scene.", file=sys.stderr)
    print(f"Tool: {tool.name}", file=sys.stderr)
    print(json.dumps(call.arguments, ensure_ascii=False, indent=2, default=str), file=sys.stderr)
    try:
        answer = input("Run this operation? Type 'yes' to approve [no]: ")
    except EOFError:
        return False
    return answer.strip().casefold() == "yes"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arena-computer-agent",
        description="Run the ArenaComputerController local AI agent against the loopback controller.",
    )
    parser.add_argument("task", nargs="+", help="Natural-language task for the local agent.")
    parser.add_argument(
        "--api-base",
        default=os.environ.get("ARENA_MODEL_BASE_URL", _DEFAULT_BASE_URL),
        help="OpenAI-compatible model API base URL (default: ARENA_MODEL_BASE_URL or Arena Router).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ARENA_MODEL_NAME", _DEFAULT_MODEL),
        help="Model name (default: ARENA_MODEL_NAME or coding-router-preview).",
    )
    parser.add_argument(
        "--controller-url",
        default=os.environ.get("ARENA_CONTROLLER_URL", "http://127.0.0.1:8765"),
        help="Loopback controller API URL (default: ARENA_CONTROLLER_URL or 127.0.0.1:8765).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=os.environ.get("ARENA_MODEL_TIMEOUT", "120"),
        help="Model request timeout in seconds (default: ARENA_MODEL_TIMEOUT or 120).",
    )
    parser.add_argument(
        "--max-turns",
        type=_integer_option("--max-turns", 1),
        default=os.environ.get("ARENA_AGENT_MAX_TURNS", "8"),
        help="Maximum model turns per task (default: 8).",
    )
    parser.add_argument(
        "--max-tool-calls",
        type=_integer_option("--max-tool-calls", 0),
        default=os.environ.get("ARENA_AGENT_MAX_TOOL_CALLS", "12"),
        help="Maximum tool calls per task (default: 12).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    api_key = os.environ.get("ARENA_MODEL_API_KEY", "")
    if not api_key.strip():
        parser.error("set ARENA_MODEL_API_KEY in the process environment; do not put API keys in source or command arguments")
    controller_token = os.environ.get("ARENA_CONTROLLER_TOKEN")

    try:
        provider = OpenAICompatibleProvider(
            base_url=args.api_base,
            api_key=api_key,
            model=args.model,
            timeout=args.timeout,
        )
        bridge = ControllerMCPBridge(
            ControllerHTTPClient(
                base_url=args.controller_url,
                token=controller_token,
            )
        )
        runtime = AgentRuntime(
            provider,
            build_controller_tools(bridge),
            approval_callback=_approve_blender_mutation,
            progress_callback=_progress,
            max_model_turns=args.max_turns,
            max_tool_calls=args.max_tool_calls,
        )
        result = runtime.run(" ".join(args.task))
    except (ValueError, ModelProviderError) as exc:
        print(f"Agent error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nAgent stopped by user.", file=sys.stderr)
        return 130

    print(result.final_text)
    if result.stop_reason != "completed":
        print(f"[agent] stopped: {result.stop_reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
