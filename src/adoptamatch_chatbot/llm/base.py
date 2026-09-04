"""Provider-neutral LLM interface.

The chatbot talks to this interface only, so swapping Anthropic for another
vendor means writing one new class and changing one line in ``cli.py`` -- not
touching the conversation loop, the MCP manager or the logger.

The interface is intentionally minimal: one call that takes a system prompt, a
history and a tool catalogue, and returns either text, a list of tool calls, or
both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    """A tool as advertised to the model. Built from an MCP ``tools/list`` entry."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """One assistant turn.

    Attributes:
        text: The visible prose, possibly empty when the model only called tools.
        tool_calls: Tool invocations the host must execute.
        raw_content: The provider's own content blocks, appended verbatim to the
            history so the next request stays valid for that provider.
        stop_reason: Provider-specific stop reason, surfaced for diagnostics.
        usage: Token accounting, when the provider reports it.
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_content: Any = None
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class LLMProvider(Protocol):
    """What the chatbot needs from a language model."""

    name: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        """Produce the next assistant turn given the full history and tool catalogue."""
        ...

    def tool_result_message(self, results: list[tuple[str, str, bool]]) -> dict[str, Any]:
        """Build the history entry carrying tool results.

        Args:
            results: ``(tool_use_id, content, is_error)`` triples, one per call in
                the assistant turn being answered.
        """
        ...


class LLMError(RuntimeError):
    """A provider failure the chatbot should report without dying."""
