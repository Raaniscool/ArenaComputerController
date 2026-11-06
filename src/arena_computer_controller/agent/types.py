"""Provider-neutral request, response, and tool types for the local agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping


MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """Image bytes that must be delivered to a model as visual input."""

    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A normalized tool request returned by a model provider."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Provider-neutral conversation message.

    Images are represented separately from text so each model adapter can map
    them to that provider's actual multimodal request format.
    """

    role: MessageRole
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    images: tuple[ImageAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """One assistant response, possibly requesting one or more tools."""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Normalized tool output returned to the agent loop."""

    data: Mapping[str, Any]
    image: ImageAttachment | None = None
    is_error: bool = False


ToolExecutor = Callable[[dict[str, Any], str], ToolResult]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Tool definition independent of any particular model API."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    execute: ToolExecutor = field(repr=False, compare=False)
    requires_approval: bool = False


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Small progress event suitable for a CLI now and a UI later."""

    kind: str
    message: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Completion summary for one task run."""

    final_text: str
    tool_call_count: int
    stop_reason: str = "completed"
