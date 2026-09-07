"""Browser front end: one Starlette app, one WebSocket protocol.

Start-up loads configuration once, the same way ``cli.py`` does. Each browser
tab that opens the WebSocket gets its own :class:`~adoptamatch_chatbot.app.ChatApp`,
:class:`~adoptamatch_chatbot.mcp_host.manager.MCPManager` and
:class:`~adoptamatch_chatbot.mcp_host.logger.InteractionLogger` -- exactly the
isolation a second terminal window would get from ``uv run adoptamatch-chatbot``,
including its own MCP subprocesses and its own session log.

One task per connection drains a queue of presenter events (populated by
:class:`~adoptamatch_chatbot.web.presenter.WebPresenter` as
``ChatApp.handle_message`` runs) and forwards them to the socket as they occur,
so a tool call and its result reach the browser live rather than batched at the
end of the turn.

WebSocket protocol, one JSON object per frame:

Client -> server
    ``{"text": "<line the user typed, including /commands>"}``

Server -> client, tagged by ``type``
    ``ready``           -- connected: session id, model, provider, log path
    ``servers``         -- one row per configured server (for ``/servers``)
    ``tools``           -- the discovered tool catalogue (for ``/tools``)
    ``logs``            -- session summary and recent frames (for ``/logs``)
    ``help``            -- the command table (for ``/help``)
    ``thinking``        -- ``state: "start" | "end"``, shown while awaiting the LLM
    ``tool_call``       -- a tool the host is about to invoke
    ``tool_result``     -- that tool's outcome
    ``assistant``       -- text to render as the model's answer
    ``warn`` / ``error`` / ``success`` / ``info`` -- a status line
    ``turn_summary``    -- tool-call count and elapsed time for the turn just finished
    ``closed``          -- the session ended (``/exit``); the socket closes next

Slash commands are handled here, not through
:meth:`ChatApp.handle_command`, because that method's ``/clear`` blocks on
``input()`` for a confirmation -- there is no terminal to read from. The browser
confirms locally (a JS ``confirm()``) and only sends ``/clear`` once agreed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from adoptamatch_chatbot import __version__
from adoptamatch_chatbot.app import COMMANDS, ChatApp
from adoptamatch_chatbot.cli import build_provider
from adoptamatch_chatbot.config import AppConfig, ConfigError, load_config
from adoptamatch_chatbot.mcp_host.logger import InteractionLogger
from adoptamatch_chatbot.mcp_host.manager import MCPManager
from adoptamatch_chatbot.web.presenter import Event, WebPresenter

STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger(__name__)


def _server_row(status: Any) -> dict[str, Any]:
    return {
        "name": status.config.name,
        "transport": status.config.transport,
        "state": status.state,
        "protocol_version": status.protocol_version,
        "tool_count": status.tool_count if status.connected else None,
        "detail": status.error or status.config.target or status.config.description,
    }


def _tool_row(tool: Any) -> dict[str, Any]:
    return {
        "exposed_name": tool.exposed_name,
        "server": tool.server,
        "original_name": tool.original_name if tool.is_qualified else None,
        "description": tool.description or "",
    }


class Session:
    """Everything one browser tab owns: its MCP connections and its ChatApp.

    Presenter events are pushed onto ``queue`` from inside ``ChatApp.handle_message``
    (via :class:`WebPresenter`); the caller is responsible for running a task that
    drains ``queue`` onto the actual WebSocket for the lifetime of the connection.
    """

    def __init__(self, config: AppConfig, offline: bool) -> None:
        self.interaction_log = InteractionLogger(config.log_dir, protocol_log=True)
        self.manager = MCPManager(config.servers, self.interaction_log, config.config_dir)
        self.provider = build_provider(config, offline)
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self.presenter = WebPresenter(self.queue)
        self.app = ChatApp(
            provider=self.provider,
            manager=self.manager,
            interaction_log=self.interaction_log,
            presenter=self.presenter,
            max_tool_iterations=config.max_tool_iterations,
        )

    async def connect(self) -> None:
        await self.manager.connect_all()

    async def close(self) -> None:
        await self.manager.aclose()

    def emit(self, event: Event) -> None:
        self.queue.put_nowait(event)

    def ready_event(self) -> Event:
        return {
            "type": "ready",
            "session_id": self.interaction_log.session_id,
            "model": self.provider.model,
            "provider": self.provider.name,
            "log_path": str(self.interaction_log.path),
        }

    def servers_event(self) -> Event:
        return {"type": "servers", "servers": [_server_row(status) for status in self.manager.statuses]}

    def tools_event(self) -> Event:
        return {"type": "tools", "tools": [_tool_row(tool) for tool in self.manager.tools]}

    def logs_event(self) -> Event:
        return {
            "type": "logs",
            "summary": self.interaction_log.summary(),
            "recent": self.interaction_log.tail(10),
        }

    def handle_command(self, command: str) -> Event:
        """The subset of ``ChatApp.handle_command`` that needs no terminal stdin."""
        if command == "/help":
            return {"type": "help", "commands": COMMANDS}
        if command == "/servers":
            return self.servers_event()
        if command == "/tools":
            return self.tools_event()
        if command == "/logs":
            return self.logs_event()
        if command == "/verbose":
            return {"type": "info", "text": "Verbose display is a browser setting; use the UI toggle."}
        if command == "/clear":
            self.app.conversation.clear()
            return {"type": "success", "text": "Context cleared. MCP connections are untouched."}
        return {"type": "warn", "text": f"Unknown command {command}. Type /help."}

    async def handle_turn(self, text: str) -> None:
        """Run one user turn. Mirrors the tail of ``ChatApp.run``'s loop body exactly:

        ``handle_message`` already catches ``LLMError`` (and ``UnknownToolError``
        per tool call) internally and reports them through the presenter, so
        nothing further needs to be caught here.
        """
        started = time.perf_counter()
        answer = await self.app.handle_message(text)
        self.presenter.assistant(answer)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.emit({"type": "turn_summary", "tool_calls": self.app.turn_tool_calls, "elapsed_ms": elapsed_ms})


async def _pump(websocket: WebSocket, queue: asyncio.Queue[Event | None]) -> None:
    """Forward queued presenter events to the browser until cancelled."""
    while True:
        event = await queue.get()
        if event is None:  # pragma: no cover - reserved for an explicit stop, unused today
            return
        await websocket.send_json(event)


async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    config: AppConfig = websocket.app.state.config
    offline: bool = websocket.app.state.offline

    session = Session(config, offline)
    pump_task = asyncio.create_task(_pump(websocket, session.queue))
    try:
        try:
            await session.connect()
        except Exception as exc:  # noqa: BLE001 - report to the browser rather than crash the socket
            session.emit({"type": "error", "text": f"Failed to start MCP servers: {exc}"})
        session.emit(session.ready_event())
        session.emit(session.servers_event())

        while True:
            raw = await websocket.receive_text()
            try:
                line = str(json.loads(raw).get("text", "")).strip()
            except (ValueError, AttributeError):
                line = raw.strip()
            if not line:
                continue

            if line.startswith("/"):
                command = line.split()[0].lower()
                if command == "/exit":
                    session.emit({"type": "closed", "text": "Session closed."})
                    break
                session.emit(session.handle_command(command))
                continue

            await session.handle_turn(line)
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
        await session.close()


async def index(request: Request) -> FileResponse:  # noqa: ARG001 - required by Starlette's Route signature
    return FileResponse(STATIC_DIR / "index.html")


def build_app(config: AppConfig, offline: bool) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", index),
            WebSocketRoute("/ws", websocket_endpoint),
        ]
    )
    app.state.config = config
    app.state.offline = offline
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adoptamatch-chatbot-web",
        description="Browser front end for the AdoptaMatch MCP host, over a WebSocket.",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Path to the .env file.")
    parser.add_argument("--servers", type=Path, default=None, help="Path to servers.toml.")
    parser.add_argument("--model", default=None, help="Override the model id for this run.")
    parser.add_argument(
        "--offline", action="store_true", help="Run without an API key using the scripted stand-in."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind. Keep this local by default.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--version", action="version", version=f"adoptamatch-chatbot-web {__version__}")
    return parser


def main() -> None:
    import uvicorn

    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        config = load_config(
            env_file=args.env_file, servers_config=args.servers, require_api_key=not args.offline
        )
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    if args.model:
        config.model = args.model

    app = build_app(config, args.offline)
    print(f"AdoptaMatch web chat: http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
