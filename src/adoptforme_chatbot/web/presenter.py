"""A ``Presenter`` that emits JSON events instead of printing to a terminal.

:class:`~adoptforme_chatbot.app.ChatApp` was written against
:class:`~adoptforme_chatbot.presentation.Presenter` and calls exactly six of its
methods from ``handle_message`` -- ``thinking``, ``error``, ``assistant``, ``warn``,
``tool_call`` and ``tool_result`` -- as plain (unawaited) calls, live, as the turn
progresses. :class:`WebPresenter` implements that same surface, so ``handle_message``
runs completely unmodified; only where the output goes changes.

Because those calls are synchronous but the browser is reached over a WebSocket,
each method pushes an event onto an ``asyncio.Queue`` instead of sending directly.
A separate task (in :mod:`adoptforme_chatbot.web.server`) drains that queue and
writes to the socket, running concurrently with ``handle_message`` on the same
event loop -- so a tool call and its result reach the browser as they happen, not
batched at the end of the turn.
"""

from __future__ import annotations

import contextlib
from asyncio import Queue
from typing import Any

from adoptforme_chatbot.mcp_host.models import ToolCallOutcome

#: One JSON-serialisable dict per UI event; see docs/architecture.md for the shape
#: of each ``type``.
Event = dict[str, Any]


class WebPresenter:
    """Turns the six calls ``ChatApp.handle_message`` makes into queued events."""

    def __init__(self, queue: Queue[Event | None]) -> None:
        self._queue = queue

    def _emit(self, event: Event) -> None:
        self._queue.put_nowait(event)

    def thinking(self, message: str = "thinking") -> Any:
        """Mirrors :meth:`Presenter.thinking`: a status the browser shows while waiting."""

        @contextlib.contextmanager
        def _status():
            self._emit({"type": "thinking", "state": "start", "message": message})
            try:
                yield
            finally:
                self._emit({"type": "thinking", "state": "end", "message": message})

        return _status()

    def error(self, message: str) -> None:
        self._emit({"type": "error", "text": message})

    def warn(self, message: str) -> None:
        self._emit({"type": "warn", "text": message})

    def assistant(self, text: str) -> None:
        if not text.strip():
            return
        self._emit({"type": "assistant", "text": text})

    def tool_call(self, name: str, server: str, arguments: dict[str, Any]) -> None:
        self._emit({"type": "tool_call", "name": name, "server": server, "arguments": arguments})

    def tool_result(self, outcome: ToolCallOutcome) -> None:
        self._emit(
            {
                "type": "tool_result",
                "tool": outcome.tool,
                "server": outcome.server,
                "ok": outcome.ok,
                "text": outcome.text,
                "elapsed_ms": outcome.elapsed_ms,
                "request_id": outcome.request_id,
            }
        )
