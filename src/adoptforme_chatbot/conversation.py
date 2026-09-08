"""Session context: the message history the LLM sees on every turn.

Context retention is not magic -- it is exactly this list. Every user message,
every assistant turn (including its ``tool_use`` blocks) and every tool result is
appended here, and the *whole* list is sent on the next request. That is why a
follow-up question like "and how much exercise does she need?" resolves "she"
correctly: the previous turn naming the animal is still in the payload.

The history is trimmed only when it grows past :attr:`Conversation.max_messages`,
and even then the trim is turn-aware: a ``tool_result`` message is never left
without the assistant ``tool_use`` message it answers, because the API rejects
that shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Keep the tail of the conversation bounded so a long demo cannot blow up the
#: request size. Large enough that ordinary multi-turn demos never hit it.
DEFAULT_MAX_MESSAGES = 60


@dataclass
class Conversation:
    """The ordered message history for one chat session."""

    max_messages: int = DEFAULT_MAX_MESSAGES
    messages: list[dict[str, Any]] = field(default_factory=list)
    user_turns: int = 0

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self.user_turns += 1
        self._trim()

    def add_assistant(self, content: Any) -> None:
        """Append the assistant turn verbatim, tool_use blocks included."""
        self.messages.append({"role": "assistant", "content": content})
        self._trim()

    def add_raw(self, message: dict[str, Any]) -> None:
        """Append a provider-built message, such as a tool-result payload."""
        self.messages.append(message)
        self._trim()

    def clear(self) -> None:
        self.messages.clear()
        self.user_turns = 0

    def snapshot(self) -> list[dict[str, Any]]:
        """A copy safe to hand to a provider without exposing internal state."""
        return list(self.messages)

    def _trim(self) -> None:
        """Drop the oldest messages, never splitting a tool_use/tool_result pair."""
        if len(self.messages) <= self.max_messages:
            return
        cut = len(self.messages) - self.max_messages
        while cut < len(self.messages) and self._is_tool_result(self.messages[cut]):
            cut += 1  # never start the history with an orphan tool_result
        self.messages = self.messages[cut:]

    @staticmethod
    def _is_tool_result(message: dict[str, Any]) -> bool:
        content = message.get("content")
        if not isinstance(content, list):
            return False
        return any(
            (isinstance(block, dict) and block.get("type") == "tool_result")
            or getattr(block, "type", None) == "tool_result"
            for block in content
        )

    def stats(self) -> dict[str, int]:
        """Counts shown by ``/help`` and ``/logs``."""
        tool_results = sum(1 for message in self.messages if self._is_tool_result(message))
        return {
            "messages": len(self.messages),
            "user_turns": self.user_turns,
            "tool_result_messages": tool_results,
        }
