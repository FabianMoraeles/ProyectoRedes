"""The MCP side of the host: connect, discover, route, call, log and shut down.

Responsibilities
----------------
* Start ``stdio`` servers as subprocesses and connect to ``streamable-http``
  servers over the network, using the official SDK's :class:`mcp.client.Client`.
* Run the handshake and ``tools/list`` against every server that comes up.
* Build a collision-free map from the tool name the LLM sees to the server that
  owns it, qualifying names as ``<server>__<tool>`` only when two servers publish
  the same one.
* Execute ``tools/call`` with a per-server timeout and turn every failure into a
  result the model can read instead of an exception that kills the session.
* Keep one server's failure isolated: a server that refuses to start is reported
  in ``/servers`` and the rest of the chatbot keeps working.
* Close every session, stream and subprocess on exit.

Isolation note: each server is entered into its **own** ``AsyncExitStack``, and
those stacks are unwound independently. That is what stops a server that hangs on
shutdown from preventing the others from closing.
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

from mcp import Implementation
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from adoptamatch_chatbot import __version__
from adoptamatch_chatbot.mcp_host.logger import InteractionLogger, new_request_id
from adoptamatch_chatbot.mcp_host.models import (
    QUALIFIER,
    ServerConfig,
    ServerStatus,
    ToolCallOutcome,
    ToolRef,
)

logger = logging.getLogger(__name__)


class UnknownToolError(LookupError):
    """Raised when the LLM asks for a tool that no connected server exposes."""


def _serialise_result(result: Any) -> tuple[str, Any]:
    """Turn a ``CallToolResult`` into (text for the LLM, structured payload).

    Preference order: the structured content when the tool published an output
    schema, otherwise the concatenated text blocks. Non-text blocks are described
    rather than dropped, so the model is never silently missing part of a result.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, default=str), structured

    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(block.text)
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            uri = getattr(resource, "uri", "?")
            parts.append(text if text is not None else f"[embedded resource: {uri}]")
        else:
            parts.append(f"[{block_type or 'unknown'} content block omitted]")
    text = "\n".join(parts).strip()
    return (text or "(the tool returned no content)"), None


class MCPManager:
    """Owns every MCP client connection for the lifetime of one chat session."""

    def __init__(
        self,
        configs: Iterable[ServerConfig],
        interaction_log: InteractionLogger,
        config_dir: Path,
    ) -> None:
        self.configs = list(configs)
        self.log = interaction_log
        self.config_dir = Path(config_dir)
        self._clients: dict[str, Client] = {}
        self._stacks: dict[str, contextlib.AsyncExitStack] = {}
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

    def _build_client(self, config: ServerConfig, mode: str) -> Client:
        """Create (but do not connect) the SDK client for one server entry."""

        async def async_message_handler(message: Any) -> None:
            self.log.log_protocol_message(config.name, config.transport, message)

        if config.transport == "stdio":
            cwd = str((self.config_dir / config.cwd).resolve()) if config.cwd else None
            parameters = StdioServerParameters(
                command=config.command or "",
                args=list(config.args),
                env=dict(config.env) or None,
                cwd=cwd,
            )
            # Build the transport explicitly so the subprocess's stderr goes to a
            # per-server file instead of the console. Reference servers are chatty
            # on stderr -- banners, deprecation notices, validation warnings for
            # methods they do not implement -- and that noise would otherwise be
            # interleaved with the conversation.
            server: Any = stdio_client(parameters, errlog=self._errlog(config.name))
        else:
            server = config.url or ""

        return Client(
            server,
            mode=mode,  # type: ignore[arg-type]
            read_timeout_seconds=config.timeout_seconds,
            message_handler=async_message_handler,
            client_info=Implementation(
                name="adoptamatch-chatbot",
                title="AdoptaMatch console host",
                version=__version__,
            ),
        )

    # ------------------------------------------------------------- connecting

    async def connect_all(self) -> None:
        """Connect every enabled server. One failure never blocks the others."""
        for config in self.configs:
            if not config.enabled:
                continue
            await self._connect_one(config)
        self._rebuild_tool_map()

    async def _open(
        self, stack: contextlib.AsyncExitStack, config: ServerConfig, mode: str
    ) -> tuple[Client, Any]:
        """Enter the client context and run the first ``tools/list``."""
        client = await stack.enter_async_context(self._build_client(config, mode))
        return client, await client.list_tools()

    async def _connect_one(self, config: ServerConfig) -> None:
        """Connect one server, falling back to the legacy handshake when needed.

        ``mode="auto"`` probes ``server/discover`` first. Some published reference
        servers neither answer that probe nor reject it, so the probe sits there
        until the read timeout expires. Rather than make every user discover that
        the hard way, an ``auto`` attempt is bounded by ``connect_timeout_seconds``
        and, if it fails, retried once with the classic ``initialize`` handshake.
        Both attempts appear in the log.
        """
        status = self._statuses[config.name]
        modes = ["legacy"] if config.mode == "legacy" else ["auto", "legacy"]
        last_error = "no connection attempt was made"

        for attempt, mode in enumerate(modes, start=1):
            request_id = new_request_id()
            started = time.perf_counter()
            self.log.log(
                server=config.name,
                transport=config.transport,
                direction="request",
                method="initialize",
                request_id=request_id,
                params={"target": config.target, "mode": mode, "attempt": attempt},
            )

            stack = contextlib.AsyncExitStack()
            try:
                client, listed = await asyncio.wait_for(
                    self._open(stack, config, mode), timeout=config.connect_timeout_seconds
                )
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - stay isolated
                await self._safe_unwind(stack, config.name)
                last_error = (
                    f"timed out after {config.connect_timeout_seconds:g}s in mode '{mode}'"
                    if isinstance(exc, (asyncio.TimeoutError, asyncio.CancelledError))
                    else f"{type(exc).__name__} in mode '{mode}': {exc}"
                )
                self.log.log(
                    server=config.name,
                    transport=config.transport,
                    direction="error",
                    method="initialize",
                    request_id=request_id,
                    error=last_error,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    status="error",
                )
                logger.warning("MCP server %r attempt %d failed: %s", config.name, attempt, last_error)
                continue

            self._clients[config.name] = client
            self._stacks[config.name] = stack
            status.connected = True
            status.error = None
            status.negotiated_mode = mode
            status.tool_count = len(listed.tools)
            status.protocol_version = client.protocol_version
            server_info = client.server_info
            status.server_title = getattr(server_info, "title", None) or getattr(server_info, "name", None)
            status.discovered_tools = list(listed.tools)

            self.log.log(
                server=config.name,
                transport=config.transport,
                direction="response",
                method="initialize",
                request_id=request_id,
                result={
                    "protocol_version": status.protocol_version,
                    "negotiated_mode": mode,
                    "server": status.server_title,
                    "tools": [tool.name for tool in listed.tools],
                },
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            return

        status.connected = False
        status.error = last_error

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
        client = self._clients.get(reference.server)
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

        if client is None:  # pragma: no cover - a disconnected server has no tools in the map
            return self._failure(reference, config, request_id, started, "server is not connected")

        try:
            result = await client.call_tool(
                reference.original_name,
                arguments,
                read_timeout_seconds=config.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - the model must see the failure, not a traceback
            return self._failure(reference, config, request_id, started, f"{type(exc).__name__}: {exc}")

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        text, structured = _serialise_result(result)
        is_error = bool(getattr(result, "is_error", False))
        self.log.log(
            server=reference.server,
            transport=config.transport,
            direction="response" if not is_error else "error",
            method="tools/call",
            request_id=request_id,
            tool=reference.original_name,
            result=structured if structured is not None else text,
            elapsed_ms=elapsed_ms,
            status="error" if is_error else "ok",
        )
        return ToolCallOutcome(
            tool=reference.exposed_name,
            server=reference.server,
            ok=not is_error,
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

    async def _safe_unwind(self, stack: contextlib.AsyncExitStack, server_name: str) -> None:
        """Unwind one stack, swallowing teardown noise from an already-dead server.

        ``CancelledError`` is caught deliberately. Closing a Streamable HTTP client
        sends a final ``DELETE`` while the transport's own anyio task group is
        already unwinding; anyio delivers that as a cancellation of the *host*
        task even though nothing outside asked us to stop. Letting it escape would
        abort the remaining servers' shutdown and lose the closing log lines. This
        method only ever runs on our own shutdown path, so swallowing it here
        cannot hide a real cancellation of ongoing work.
        """
        try:
            await stack.aclose()
        except asyncio.CancelledError:
            logger.debug("Teardown of %r was cancelled by its own transport.", server_name)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.debug("Ignoring teardown error for %r: %s", server_name, exc)

    async def aclose(self) -> None:
        """Close every session, stream and subprocess. Safe to call twice."""
        for name in list(self._stacks):
            stack = self._stacks.pop(name)
            self._clients.pop(name, None)
            await self._safe_unwind(stack, name)
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
        return sorted(self._clients)
