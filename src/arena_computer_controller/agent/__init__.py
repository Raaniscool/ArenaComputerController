"""Independent local AI-agent runtime for ArenaComputerController."""

from .openai_compatible import ModelProviderError, OpenAICompatibleProvider
from .runtime import AgentRuntime, DEFAULT_SYSTEM_PROMPT
from .tools import ToolRegistry, build_controller_tools
from .types import (
    AgentEvent,
    AgentResult,
    AssistantTurn,
    ChatMessage,
    ImageAttachment,
    ToolCall,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "AgentEvent",
    "AgentResult",
    "AgentRuntime",
    "AssistantTurn",
    "ChatMessage",
    "DEFAULT_SYSTEM_PROMPT",
    "ImageAttachment",
    "ModelProviderError",
    "OpenAICompatibleProvider",
    "ToolCall",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_controller_tools",
]
