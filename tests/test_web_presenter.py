"""WebPresenter: the same six calls ChatApp.handle_message makes, as JSON events.

ChatApp itself is not re-tested here (tests/test_app.py already drives it against
a ScriptedProvider); what matters for the web front end is that WebPresenter is a
faithful stand-in for Presenter's subset ChatApp actually calls, and that the
events it queues have the shape the browser (static/index.html) expects.
"""

from __future__ import annotations

import asyncio

import pytest

from adoptamatch_chatbot.mcp_host.models import ToolCallOutcome
from adoptamatch_chatbot.web.presenter import WebPresenter


def drain(queue: asyncio.Queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


class TestStatusLines:
    def test_error_is_queued_with_its_text(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        WebPresenter(queue).error("boom")
        assert drain(queue) == [{"type": "error", "text": "boom"}]

    def test_warn_is_queued_with_its_text(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        WebPresenter(queue).warn("careful")
        assert drain(queue) == [{"type": "warn", "text": "careful"}]


class TestAssistantText:
    def test_blank_text_is_never_queued(self) -> None:
        """Mirrors Presenter.assistant: a narration-free turn sends nothing."""
        queue: asyncio.Queue = asyncio.Queue()
        WebPresenter(queue).assistant("   ")
        assert drain(queue) == []

    def test_real_text_is_queued_verbatim(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        WebPresenter(queue).assistant("Paco is a great match.")
        assert drain(queue) == [{"type": "assistant", "text": "Paco is a great match."}]


class TestToolEvents:
    def test_a_tool_call_carries_its_server_name_and_arguments(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        WebPresenter(queue).tool_call("recommend_animals", "adoptamatch", {"limit": 3})
        assert drain(queue) == [
            {
                "type": "tool_call",
                "name": "recommend_animals",
                "server": "adoptamatch",
                "arguments": {"limit": 3},
            }
        ]

    def test_a_tool_result_flattens_the_outcome_dataclass(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        outcome = ToolCallOutcome(
            tool="recommend_animals",
            server="adoptamatch",
            ok=True,
            text="fine",
            elapsed_ms=42,
            request_id="abc123",
        )
        WebPresenter(queue).tool_result(outcome)
        assert drain(queue) == [
            {
                "type": "tool_result",
                "tool": "recommend_animals",
                "server": "adoptamatch",
                "ok": True,
                "text": "fine",
                "elapsed_ms": 42,
                "request_id": "abc123",
            }
        ]

    def test_a_failed_result_still_reports_ok_false(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        outcome = ToolCallOutcome(tool="t", server="s", ok=False, text="nope", elapsed_ms=1, request_id="x")
        WebPresenter(queue).tool_result(outcome)
        assert drain(queue)[0]["ok"] is False


class TestThinking:
    def test_entering_and_leaving_queues_start_then_end(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        with WebPresenter(queue).thinking("asking the model"):
            assert drain(queue) == [{"type": "thinking", "state": "start", "message": "asking the model"}]
        assert drain(queue) == [{"type": "thinking", "state": "end", "message": "asking the model"}]

    def test_the_end_event_is_queued_even_if_the_body_raises(self) -> None:
        """A failed LLM call must not leave the browser's spinner running forever."""
        queue: asyncio.Queue = asyncio.Queue()
        with pytest.raises(RuntimeError), WebPresenter(queue).thinking():
            raise RuntimeError("network down")
        events = drain(queue)
        assert events[-1] == {"type": "thinking", "state": "end", "message": "thinking"}
