"""JSON-RPC 2.0 message construction, parsing and classification.

This is the bottom of the client stack. No MCP SDK is used anywhere in the
``mcp_wire`` package: every frame the host sends or receives is built and parsed
here, against the two specifications the project rests on:

* JSON-RPC 2.0  https://www.jsonrpc.org/specification
* MCP           https://modelcontextprotocol.io/specification/2025-06-18

The four message kinds matter for the network analysis, so they get first-class
names here and the same names appear in the wire log and in the report:

===============  ==========================================  ====================
Kind             Shape                                        Role in MCP
===============  ==========================================  ====================
``request``      ``method`` + ``id``                          a call awaiting a reply
``notification`` ``method``, **no** ``id``                     fire-and-forget
``response``     ``id`` + ``result``                          a successful reply
``error``        ``id`` + ``error``                           a protocol-level failure
===============  ==========================================  ====================
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 reserved error codes, for readable diagnostics.
ERROR_CODES = {
    -32700: "Parse error",
    -32600: "Invalid request",
    -32601: "Method not found",
    -32602: "Invalid params",
    -32603: "Internal error",
}

#: MCP lifecycle methods. Separated from the rest because the assignment asks
#: which captured messages are synchronisation and which are ordinary calls.
LIFECYCLE_METHODS = frozenset({"initialize", "notifications/initialized", "ping"})


class ProtocolError(RuntimeError):
    """The peer returned a JSON-RPC error, or broke the framing."""

    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

    @property
    def code_name(self) -> str:
        return ERROR_CODES.get(self.code or 0, "Server error")


@dataclass
class IdAllocator:
    """Monotonic request ids.

    JSON-RPC only requires that an id be unique within a connection and not
    ``null``. A simple counter makes a capture trivially readable: the *n*-th
    request the host sent carries id *n*.
    """

    _counter: itertools.count = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._counter = itertools.count(1)

    def next(self) -> int:
        return next(self._counter)


def request(method: str, params: dict[str, Any] | None, request_id: int | str) -> dict[str, Any]:
    """Build a request: it has an ``id``, so exactly one reply is expected."""
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a notification: no ``id``, so the peer must never reply."""
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


def classify(message: dict[str, Any]) -> str:
    """Return ``request``, ``notification``, ``response``, ``error`` or ``unknown``."""
    if "method" in message:
        return "request" if "id" in message else "notification"
    if "error" in message:
        return "error"
    if "result" in message:
        return "response"
    return "unknown"


def is_lifecycle(message: dict[str, Any]) -> bool:
    """True for the handshake and keep-alive messages.

    Used by the wire log so the capture can be split into "synchronisation"
    versus "ordinary call", which is exactly the classification the network
    analysis asks for.
    """
    return message.get("method") in LIFECYCLE_METHODS


def raise_for_error(message: dict[str, Any]) -> dict[str, Any]:
    """Return the ``result``, or raise :class:`ProtocolError` for an error reply."""
    if "error" in message:
        error = message["error"] or {}
        code = error.get("code")
        detail = error.get("message", "unknown error")
        label = ERROR_CODES.get(code, "Server error")
        raise ProtocolError(f"{label} ({code}): {detail}", code, error.get("data"))
    if "result" not in message:
        raise ProtocolError("Reply carried neither 'result' nor 'error'; the peer is not conforming.")
    return message["result"]
