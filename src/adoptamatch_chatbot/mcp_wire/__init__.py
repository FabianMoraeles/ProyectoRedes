"""A hand-written MCP client: JSON-RPC 2.0 over stdio and Streamable HTTP.

No MCP SDK is used. See :mod:`adoptamatch_chatbot.mcp_wire.messages` for the
framing, :mod:`~adoptamatch_chatbot.mcp_wire.transports` for the two transports
and :mod:`~adoptamatch_chatbot.mcp_wire.session` for the protocol lifecycle.
"""

from adoptamatch_chatbot.mcp_wire.messages import ProtocolError, classify, is_lifecycle
from adoptamatch_chatbot.mcp_wire.session import (
    PREFERRED_PROTOCOL_VERSION,
    ClientSession,
    ServerIdentity,
    ToolDescriptor,
    ToolResult,
)
from adoptamatch_chatbot.mcp_wire.transports import (
    StdioTransport,
    StreamableHttpTransport,
    Transport,
    TransportError,
)

__all__ = [
    "PREFERRED_PROTOCOL_VERSION",
    "ClientSession",
    "ProtocolError",
    "ServerIdentity",
    "StdioTransport",
    "StreamableHttpTransport",
    "ToolDescriptor",
    "ToolResult",
    "Transport",
    "TransportError",
    "classify",
    "is_lifecycle",
]
