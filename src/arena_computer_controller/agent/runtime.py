"""Single-agent tool-calling loop for ArenaComputerController."""

from __future__ import annotations

import json
from typing import Callable, Sequence

from .provider import ModelProvider
from .tools import ToolRegistry
from .types import AgentEvent, AgentResult, AssistantTurn, ChatMessage, ImageAttachment, ToolCall, ToolResult, ToolSpec

ApprovalCallback = Callable[[ToolSpec, ToolCall], bool]
ProgressCallback = Callable[[AgentEvent], None]

DEFAULT_SYSTEM_PROMPT = """You are ArenaComputerController, an AI agent operating the user's local computer through explicitly listed tools.

For a task that asks you to inspect the screen, first call get_computer_state, then call get_screenshot. A screenshot tool result may be attached as an actual image in the following message; inspect its pixels before describing what you see. A URL, path, hash, or metadata alone is not visual inspection. If no image payload arrives, say that you could not inspect the screenshot.

Use only the supplied tools. Do not invent computer, window, or Blender state. Do not perform a Blender change unless the user requested it; every blender_execute call also requires a separate approval from the user. Report tool failures honestly and summarize what you observed."""

_SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}


class AgentRuntime:
    """Orchestrate model turns, local tools, results, and image feedback.

    The runtime is provider-neutral. The provider only decides the next assistant
    turn; tools remain local and are dispatched here, with a hard call budget.
    """

    def __init__(
        self,
        provider: ModelProvider,
        tools: Sequence[ToolSpec],
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        approval_callback: ApprovalCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        max_model_turns: int = 8,
        max_tool_calls: int = 12,
    ):
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("System prompt must be a non-empty string.")
        if isinstance(max_model_turns, bool) or not isinstance(max_model_turns, int) or max_model_turns < 1:
            raise ValueError("max_model_turns must be a positive integer.")
        if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int) or max_tool_calls < 0:
            raise ValueError("max_tool_calls must be a non-negative integer.")
        self.provider = provider
        self.registry = ToolRegistry(tools)
        self.system_prompt = system_prompt
        self.approval_callback = approval_callback
        self.progress_callback = progress_callback
        self.max_model_turns = max_model_turns
        self.max_tool_calls = max_tool_calls

    def run(self, task: str) -> AgentResult:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Task must be a non-empty string.")
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(role="user", content=task.strip()),
        ]
        tool_call_count = 0
        self._emit(AgentEvent("task_started", "Starting the task."))

        for model_turn in range(1, self.max_model_turns + 1):
            self._emit(AgentEvent("model_request", f"Requesting model response (turn {model_turn})."))
            available_tools = self.registry.tools if tool_call_count < self.max_tool_calls else ()
            turn = self.provider.complete(messages, available_tools)
            if not isinstance(turn, AssistantTurn):
                raise TypeError("Model provider must return an AssistantTurn.")
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=turn.content,
                    tool_calls=turn.tool_calls,
                )
            )
            self._emit(
                AgentEvent(
                    "model_response",
                    "Model requested tools." if turn.tool_calls else "Model returned a final response.",
                    data={"tool_names": [call.name for call in turn.tool_calls]},
                )
            )

            if not turn.tool_calls:
                final_text = turn.content or ""
                self._emit(AgentEvent("task_completed", "Task completed."))
                return AgentResult(final_text=final_text, tool_call_count=tool_call_count)

            image_messages: list[ChatMessage] = []
            # Keep every tool result adjacent to its assistant tool-call turn. If
            # a provider returns more than one call despite sequential mode, send
            # all tool replies before adding any user image messages.
            for call in turn.tool_calls:
                if tool_call_count >= self.max_tool_calls:
                    message = "Stopped because the configured tool-call limit was reached."
                    self._emit(AgentEvent("tool_limit", message, tool_name=call.name, tool_call_id=call.id))
                    return AgentResult(
                        final_text=(turn.content or "").strip() or message,
                        tool_call_count=tool_call_count,
                        stop_reason="max_tool_calls",
                    )
                tool_call_count += 1
                result = self._execute(call)
                result_text = self._serialize_result(result)
                messages.append(
                    ChatMessage(
                        role="tool",
                        name=call.name,
                        tool_call_id=call.id,
                        content=result_text,
                    )
                )
                if result.image is not None:
                    image_description = (
                        f"Actual image content returned by tool {call.name}. "
                        "Inspect the image itself and use it as visual evidence; the adjacent tool result contains its metadata."
                    )
                    image_messages.append(
                        ChatMessage(
                            role="user",
                            content=image_description,
                            images=(result.image,),
                        )
                    )
                self._emit(
                    AgentEvent(
                        "tool_completed",
                        "Tool returned an image." if result.image else "Tool returned a result.",
                        tool_name=call.name,
                        tool_call_id=call.id,
                        data={
                            "is_error": result.is_error,
                            "image_included": result.image is not None,
                            "image_mime_type": result.image.mime_type if result.image is not None else None,
                            "image_bytes": len(result.image.data) if result.image is not None else 0,
                        },
                    )
                )
            messages.extend(image_messages)

        message = "Stopped because the configured model-turn limit was reached."
        self._emit(AgentEvent("turn_limit", message))
        return AgentResult(final_text=message, tool_call_count=tool_call_count, stop_reason="max_model_turns")

    def _execute(self, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        self._emit(
            AgentEvent(
                "tool_started",
                f"Calling {call.name}.",
                tool_name=call.name,
                tool_call_id=call.id,
                data={"requires_approval": bool(tool and tool.requires_approval)},
            )
        )
        if tool is None:
            return ToolResult(
                {"ok": False, "error": {"code": "unknown_tool", "message": "The requested tool is not available."}},
                is_error=True,
            )
        if not isinstance(call.arguments, dict):
            return ToolResult(
                {"ok": False, "error": {"code": "invalid_arguments", "message": "Tool arguments must be a JSON object."}},
                is_error=True,
            )
        if tool.requires_approval:
            approved = False
            if self.approval_callback is not None:
                try:
                    approved = self.approval_callback(tool, call) is True
                except Exception:
                    approved = False
            if not approved:
                self._emit(
                    AgentEvent(
                        "approval_denied",
                        f"User approval was not granted for {call.name}; no action was taken.",
                        tool_name=call.name,
                        tool_call_id=call.id,
                    )
                )
                return ToolResult(
                    {"ok": False, "error": {"code": "approval_denied", "message": "The user did not approve this action; it was not run."}},
                    is_error=True,
                )
        try:
            result = tool.execute(call.arguments, call.id)
        except Exception as exc:
            # Do not pass arbitrary exception text (which may contain local paths
            # or secrets) to the model. Tool adapters should return safe failures.
            return ToolResult(
                {
                    "ok": False,
                    "error": {
                        "code": "tool_execution_error",
                        "message": f"The local tool failed ({type(exc).__name__}); details were withheld.",
                    },
                },
                is_error=True,
            )
        if not isinstance(result, ToolResult):
            return ToolResult(
                {"ok": False, "error": {"code": "invalid_tool_result", "message": "The tool returned an invalid result."}},
                is_error=True,
            )
        if result.image is not None:
            if (
                not isinstance(result.image, ImageAttachment)
                or result.image.mime_type not in _SUPPORTED_IMAGE_TYPES
                or not isinstance(result.image.data, bytes)
                or not result.image.data
            ):
                return ToolResult(
                    {"ok": False, "error": {"code": "invalid_image_result", "message": "The tool returned an invalid image payload."}},
                    is_error=True,
                )
        return result

    @staticmethod
    def _serialize_result(result: ToolResult) -> str:
        try:
            data = dict(result.data)
            if result.is_error:
                data["is_error"] = True
            return json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
                default=str,
            )
        except (TypeError, ValueError, RecursionError):
            return '{"ok":false,"error":{"code":"result_not_serializable","message":"Tool result could not be encoded."}}'

    def _emit(self, event: AgentEvent) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event)
        except Exception:
            # Progress/UI observers must not abort a computer-control task.
            pass
