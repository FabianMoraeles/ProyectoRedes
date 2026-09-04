"""The MCP side of the host: connect, discover, route, call, log and shut down.

The protocol itself is implemented in :mod:`adoptamatch_chatbot.mcp_wire`,
directly over JSON-RPC 2.0. **No MCP SDK is used.** This module is the layer that
turns a list of configured servers into one tool catalogue the model can use.

Responsibilities
----------------
* Start ``stdio`` servers as subprocesses and connect to ``streamable-http``
  servers over TCP.
* Run the ``initialize`` / ``notifications/initialized`` handshake and
  ``tools/list`` against every server that comes up.
* Build a collision-free map from the tool name the LLM sees to the server that
  owns it, qualifying names as ``<server>__<tool>`` only when two servers publish
  the same one.
* Execute ``tools/call`` with a per-server timeout and turn every failure into a
  result the model can read instead of an exception that kills the session.
* Keep one server's failure isolated: a server that refuses to start is reported
  in ``/servers`` and the rest of the chatbot keeps working.
* Close every session, stream and subprocess on exit.

Two logs come out of this. The host-level log records one event per request and
one per reply, correlated by ``request_id``. The wire log records every JSON-RPC
frame in both directions -- possible only because the host writes those bytes
itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TextIO

from adoptamatch_chatbot.mcp_host.logger import InteractionLogger, new_request_id
from adoptamatch_chatbot.mcp_host.models import (
    QUALIFIER,
    ServerConfig,
    ServerStatus,
    ToolCallOutcome,
    ToolRef,
)
from adoptamatch_chatbot.mcp_wire import (
    ClientSession,
    ProtocolError,
    StdioTransport,
    StreamableHttpTransport,
    TransportError,
)

logger = logging.getLogger(__name__)


class UnknownToolError(LookupError):
    """Raised when the LLM asks for a tool that no connected server exposes."""


class MCPManager:
    """Owns every MCP client session for the lifetime of one chat session."""

    def __init__(
        self,
        configs: Iterable[ServerConfig],
        interaction_log: InteractionLogger,
        config_dir: Path,
    ) -> None:
        self.configs = list(configs)
        self.log = interaction_log
        self.config_dir = Path(config_dir)
        self._sessions: dict[str, ClientSession] = {}
        self._errlogs: dict[str, TextIO] = {}
        self._statuses: dict[str, ServerStatus] = {
            config.name: ServerStatus(config=config) for config in self.configs
        }
        self._tools: dict[str, ToolRef] = {}

    # ------------------------------------------------------------------ wiring

    def _errlog(self, server_name: str) -> TextIO:
        """Return (opening once per session) the stderr sink for one stdio server."""
        handle = self._errlogs.get(server_name)
        if handle is None:
            path = self.log.log_dir / f"session-{self.log.session_id}.{server_name}.stderr.log"
            handle = path.open("a", encoding="utf-8", errors="replace")
            self._errlogs[server_name] = handle
        return handle

    def _build_session(self, config: ServerConfig) -> ClientSession:
        """Create (but do not connect) the client session for one server entry."""

        def on_frame(direction: str, message: dict[str, Any]) -> None:
            self.log.log_frame(config.name, config.transport, direction, message)

        if config.transport == "stdio":
            cwd = str((self.config_dir / config.cwd).resolve()) if config.cwd else None
            # The subprocess's stderr goes to a per-server file rather than the
            # console: reference servers are chatty (banners, deprecation notices,
            # warnings about methods they do not implement) and that noise would
            # otherwise be interleaved with the conversation.
            transport: Any = StdioTransport(
                command=config.command or "",
                args=list(config.args),
                env=dict(config.env),
                cwd=cwd,
                errlog=self._errlog(config.name),
                on_frame=on_frame,
            )
        else:
            transport = StreamableHttpTransport(
                url=config.url or "",
                on_frame=on_frame,
                connect_timeout=config.connect_timeout_seconds,
            )
        return ClientSession(transport, timeout=config.timeout_seconds)

    # ------------------------------------------------------------- connecting

    async def connect_all(self) -> None:
        """Connect every enabled server. One failure never blocks the others."""
        for config in self.configs:
            if not config.enabled:
                continue
            await self._connect_one(config)
        self._rebuild_tool_map()

    async def _open(self, session: ClientSession) -> Any:
        """Handshake, then the first ``tools/list``."""
        identity = await session.initialize()
        return identity, await session.list_tools()

    async def _connect_one(self, config: ServerConfig) -> None:
        """Connect one server, bounded by ``connect_timeout_seconds``.

        There is no negotiation guesswork here: a hand-written client sends
        ``initialize`` first, exactly as the specification prescribes, so a server
        either completes the handshake or fails for a reason worth reporting.
        """
        status = self._statuses[config.name]
        request_id = new_request_id()
        started = time.perf_counter()
        self.log.log(
            server=config.name,
            transport=config.transport,
            direction="request",
            method="initialize",
            request_id=request_id,
            params={"target": config.target},
        )

        session = self._build_session(config)
        try:
            identity, tools = await asyncio.wait_for(
                self._open(session), timeout=config.connect_timeout_seconds
            )
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - stay isolated
            await self._safe_close(session, config.name)
            reason = (
                f"timed out after {config.connect_timeout_seconds:g}s during the handshake"
                if isinstance(exc, (asyncio.TimeoutError, asyncio.CancelledError))
                else f"{type(exc).__name__}: {exc}"
            )
            status.connected = False
            status.error = reason
            self.log.log(
                server=config.name,
                transport=config.transport,
                direction="error",
                method="initialize",
                request_id=request_id,
                error=reason,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                status="error",
            )
            logger.warning("MCP server %r failed to start: %s", config.name, reason)
            return

        self._sessions[config.name] = session
        status.connected = True
        status.error = None
        status.tool_count = len(tools)
        status.protocol_version = identity.protocol_version
        status.server_title = identity.title or identity.name
        status.instructions = identity.instructions
        status.discovered_tools = list(tools)

        self.log.log(
            server=config.name,
            transport=config.transport,
            direction="response",
            method="initialize",
            request_id=request_id,
            result={
                "protocol_version": status.protocol_version,
                "server": status.server_title,
                "tools": [tool.name for tool in tools],
            },
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def _rebuild_tool_map(self) -> None:
        """Map exposed tool name -> ToolRef, qualifying only on a real collision."""
        discovered: list[tuple[str, Any]] = []
        for name, status in self._statuses.items():
            for tool in status.discovered_tools:
                discovered.append((name, tool))

        counts: dict[str, int] = {}
        for _, tool in discovered:
            counts[tool.name] = counts.get(tool.name, 0) + 1

        self._tools = {}
        for server_name, tool in discovered:
            exposed = tool.name if counts[tool.name] == 1 else f"{server_name}{QUALIFIER}{tool.name}"
            if exposed in self._tools:  # pragma: no cover - two servers with the same name
                exposed = f"{server_name}{QUALIFIER}{tool.name}{QUALIFIER}{len(self._tools)}"
            self._tools[exposed] = ToolRef(
                server=server_name,
                original_name=tool.name,
                exposed_name=exposed,
                description=tool.description or "",
                input_schema=tool.input_schema,
            )

    # ----------------------------------------------------------------- calling

    async def call_tool(self, exposed_name: str, arguments: dict[str, Any]) -> ToolCallOutcome:
        """Route one tool call to its owning server, with logging and a timeout."""
        reference = self._tools.get(exposed_name)
        if reference is None:
            raise UnknownToolError(
                f"No connected MCP server exposes a tool named '{exposed_name}'. "
                f"Available: {', '.join(sorted(self._tools)) or '(none)'}"
            )

        config = self._statuses[reference.server].config
        session = self._sessions.get(reference.server)
        request_id = new_request_id()
        self.log.log(
            server=reference.server,
            transport=config.transport,
            direction="request",
            method="tools/call",
            request_id=request_id,
            tool=reference.original_name,
            params=arguments,
        )
        started = time.perf_counter()

        if session is None:  # pragma: no cover - a disconnected server has no tools in the map
            return self._failure(reference, config, request_id, started, "server is not connected")

        try:
            result = await session.call_tool(
                reference.original_name, arguments, timeout=config.timeout_seconds
            )
        except (TransportError, ProtocolError) as exc:
            return self._failure(reference, config, request_id, started, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - the model must see the failure, not a traceback
            return self._failure(reference, config, request_id, started, f"{type(exc).__name__}: {exc}")

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        structured = result.structured
        text = result.text
        if structured is not None and not text:
            text = json.dumps(structured, ensure_ascii=False, default=str)
        self.log.log(
            server=reference.server,
            transport=config.transport,
            direction="error" if result.is_error else "response",
            method="tools/call",
            request_id=request_id,
            tool=reference.original_name,
            result=structured if structured is not None else text,
            elapsed_ms=elapsed_ms,
            status="error" if result.is_error else "ok",
        )
        return ToolCallOutcome(
            tool=reference.exposed_name,
            server=reference.server,
            ok=not result.is_error,
            text=text,
            structured=structured,
            elapsed_ms=elapsed_ms,
            request_id=request_id,
        )

    def _failure(
        self,
        reference: ToolRef,
        config: ServerConfig,
        request_id: str,
        started: float,
        message: str,
    ) -> ToolCallOutcome:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.log.log(
            server=reference.server,
            transport=config.transport,
            direction="error",
            method="tools/call",
            request_id=request_id,
            tool=reference.original_name,
            error=message,
            elapsed_ms=elapsed_ms,
            status="error",
        )
        return ToolCallOutcome(
            tool=reference.exposed_name,
            server=reference.server,
            ok=False,
            text=f"The tool call failed: {message}",
            elapsed_ms=elapsed_ms,
            request_id=request_id,
        )

    # ---------------------------------------------------------------- teardown

    async def _safe_close(self, session: ClientSession, server_name: str) -> None:
        """Close one session, swallowing teardown noise from an already-dead peer."""
        try:
            await session.aclose()
        except asyncio.CancelledError:
            logger.debug("Teardown of %r was cancelled by its own transport.", server_name)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.debug("Ignoring teardown error for %r: %s", server_name, exc)

    async def aclose(self) -> None:
        """Close every session, stream and subprocess. Safe to call twice."""
        for name in list(self._sessions):
            session = self._sessions.pop(name)
            await self._safe_close(session, name)
            status = self._statuses.get(name)
            if status is not None:
                status.connected = False
            self.log.log(
                server=name,
                transport=self._statuses[name].config.transport,
                direction="lifecycle",
                method="shutdown",
                request_id=new_request_id(),
                result={"closed": True},
            )

        for handle in self._errlogs.values():
            with contextlib.suppress(OSError):
                handle.close()
        self._errlogs.clear()

    # ------------------------------------------------------------ introspection

    @property
    def tools(self) -> list[ToolRef]:
        return sorted(self._tools.values(), key=lambda ref: (ref.server, ref.exposed_name))

    @property
    def statuses(self) -> list[ServerStatus]:
        return [self._statuses[config.name] for config in self.configs]

    @property
    def connected_servers(self) -> list[str]:
        return sorted(self._sessions)
