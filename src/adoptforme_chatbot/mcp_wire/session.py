"""An MCP client session, implemented directly over JSON-RPC.

This is the piece the assignment's first extra asks for: the host speaks the
protocol itself rather than delegating to an MCP SDK. The lifecycle is exactly
what the specification prescribes, and each step is one JSON-RPC message you can
point at in a packet capture:

1. ``initialize`` (request)  - propose a protocol revision, declare capabilities,
   identify the client. The server answers with the revision it will actually use,
   its own capabilities, its identity, and optional usage ``instructions``.
2. ``notifications/initialized`` (notification) - the client confirms. No reply,
   because a notification carries no ``id``.
3. ``tools/list`` (request) - discover the catalogue and its JSON Schemas.
4. ``tools/call`` (request) - invoke one tool.
5. ``ping`` (request) - optional liveness check.

Steps 1 and 2 are *synchronisation*; 3, 4 and 5 are ordinary calls. That split is
carried through into the wire log and into the network report.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from adoptforme_chatbot.mcp_wire.messages import (
    IdAllocator,
    ProtocolError,
    classify,
    notification,
    raise_for_error,
    request,
)
from adoptforme_chatbot.mcp_wire.transports import Transport, TransportError

logger = logging.getLogger(__name__)

#: Revisions this client is willing to speak, newest first.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
PREFERRED_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

CLIENT_NAME = "adoptforme-chatbot"


@dataclass
class ToolDescriptor:
    """One entry of a ``tools/list`` result, normalised for the host."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    title: str | None = None

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> ToolDescriptor:
        schema = payload.get("inputSchema") or {"type": "object", "properties": {}}
        return cls(
            name=payload["name"],
            description=payload.get("description") or "",
            input_schema=schema,
            output_schema=payload.get("outputSchema"),
            title=payload.get("title"),
        )


@dataclass
class ToolResult:
    """One ``tools/call`` result, normalised for the host."""

    is_error: bool
    text: str
    structured: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServerIdentity:
    """What the server told us about itself during the handshake."""

    name: str | None = None
    title: str | None = None
    version: str | None = None
    protocol_version: str | None = None
    instructions: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


def _flatten_content(blocks: list[dict[str, Any]] | None) -> str:
    """Concatenate the text blocks of a result, describing anything else."""
    parts: list[str] = []
    for block in blocks or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "resource":
            resource = block.get("resource") or {}
            parts.append(resource.get("text") or f"[embedded resource: {resource.get('uri', '?')}]")
        else:
            parts.append(f"[{kind or 'unknown'} content block omitted]")
    return "\n".join(part for part in parts if part).strip()


class ClientSession:
    """One MCP conversation with one server, over one transport."""

    def __init__(self, transport: Transport, timeout: float = 60.0) -> None:
        self.transport = transport
        self.timeout = timeout
        self.ids = IdAllocator()
        self.identity = ServerIdentity()
        self.initialized = False

    # ------------------------------------------------------------- primitives

    async def _call(self, method: str, params: dict[str, Any] | None, timeout: float | None) -> Any:
        message = request(method, params, self.ids.next())
        reply = await self.transport.send_request(message, timeout or self.timeout)
        kind = classify(reply)
        if kind not in ("response", "error"):
            raise ProtocolError(f"Expected a reply to {method!r}, got a {kind}.")
        if reply.get("id") != message["id"]:
            raise ProtocolError(f"Reply id {reply.get('id')!r} does not match request id {message['id']!r}.")
        return raise_for_error(reply)

    # -------------------------------------------------------------- lifecycle

    async def initialize(self) -> ServerIdentity:
        """Run the handshake and record what the server said about itself."""
        await self.transport.start()
        result = await self._call(
            "initialize",
            {
                "protocolVersion": PREFERRED_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": _client_version()},
            },
            timeout=self.timeout,
        )
        server_info = result.get("serverInfo") or {}
        self.identity = ServerIdentity(
            name=server_info.get("name"),
            title=server_info.get("title"),
            version=server_info.get("version"),
            protocol_version=result.get("protocolVersion"),
            instructions=result.get("instructions"),
            capabilities=result.get("capabilities") or {},
        )
        if self.identity.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            # The specification allows the server to answer with a revision we did
            # not propose. Continue, but say so: a mismatch explains later
            # surprises far better than a silent success.
            logger.warning(
                "Server negotiated protocol %r, which this client does not list as supported.",
                self.identity.protocol_version,
            )
        # Propagate the negotiated revision to an HTTP transport, which must send
        # it as a header on every subsequent request.
        if hasattr(self.transport, "protocol_version"):
            self.transport.protocol_version = self.identity.protocol_version  # type: ignore[attr-defined]

        await self.transport.send_notification(notification("notifications/initialized"))
        self.initialized = True
        return self.identity

    async def ping(self) -> None:
        await self._call("ping", None, timeout=min(self.timeout, 10.0))

    # ------------------------------------------------------------------ tools

    async def list_tools(self) -> list[ToolDescriptor]:
        """Discover the catalogue, following ``nextCursor`` pagination if present."""
        tools: list[ToolDescriptor] = []
        cursor: str | None = None
        for _ in range(50):  # a hard stop, so a broken server cannot loop forever
            params = {"cursor": cursor} if cursor else None
            result = await self._call("tools/list", params, timeout=self.timeout)
            tools.extend(ToolDescriptor.from_wire(entry) for entry in result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, timeout: float | None = None
    ) -> ToolResult:
        """Invoke one tool.

        A failure *inside* the tool comes back as a normal result with
        ``isError: true`` -- that is the specification's design, so the model can
        read the message. A failure of the *protocol* raises instead.
        """
        result = await self._call("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        structured = result.get("structuredContent")
        # When a tool publishes an output schema, `structuredContent` is the
        # canonical data and `content` is only a rendering of it. Hand the model
        # the structured form so every tool result has one predictable shape.
        if structured is not None:
            text = json.dumps(structured, ensure_ascii=False, default=str)
        else:
            text = _flatten_content(result.get("content"))
        return ToolResult(
            is_error=bool(result.get("isError")),
            text=text or "(the tool returned no content)",
            structured=structured,
            raw=result,
        )

    # --------------------------------------------------------------- shutdown

    async def aclose(self) -> None:
        try:
            await self.transport.aclose()
        except TransportError:  # pragma: no cover - shutdown must not raise
            logger.debug("Transport reported an error while closing; ignoring.")


def _client_version() -> str:
    from adoptforme_chatbot import __version__

    return __version__
