"""Interfaces implemented by model providers."""

from __future__ import annotations

from typing import Protocol, Sequence

from .types import AssistantTurn, ChatMessage, ToolSpec


class ModelProvider(Protocol):
    """A model API adapter used by the provider-independent agent runtime."""

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> AssistantTurn:
        """Return one assistant turn; tool execution remains the runtime's job."""
        ...
