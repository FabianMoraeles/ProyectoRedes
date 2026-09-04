"""minimcp - a Model Context Protocol server implemented directly over JSON-RPC 2.0.

No MCP SDK is used anywhere in this file. Every message is built, framed, parsed
and dispatched here, against the protocol specification:

* MCP           https://modelcontextprotocol.io/specification/2025-06-18
* JSON-RPC 2.0  https://www.jsonrpc.org/specification

VENDORED FILE. The canonical copy lives in the public repository
``adoptamatch-mcp`` at ``src/adoptamatch_mcp/minimcp.py``. The copy inside
``pet_care_mcp`` is byte-identical on purpose: the two servers must be
independently installable, and a single shared file is easier to audit than two
divergent implementations. Change one, copy to the other.

What it implements
------------------
=========================  ============================================================
``initialize``             Version negotiation + capability exchange (a *lifecycle*
                           message, not a tool call).
``notifications/initialized``  Client's acknowledgement. No response is sent, because a
                           JSON-RPC notification carries no ``id``.
``ping``                   Liveness check, empty result.
``tools/list``             Tool catalogue with JSON Schemas.
``tools/call``             Tool invocation.
=========================  ============================================================

Anything else gets a JSON-RPC ``-32601 Method not found`` error, which is what a
conforming server must do.

Two transports
--------------
* :func:`run_stdio` - newline-delimited JSON on stdin/stdout. No socket, so nothing
  for a packet capture to see.
* :func:`run_http` - Streamable HTTP: JSON-RPC in the body of ``POST /mcp``, with a
  ``Mcp-Session-Id`` header issued at ``initialize`` and echoed afterwards. This is
  the transport that actually runs over TCP.

Schema generation uses pydantic, which is a data-validation library, not an MCP
SDK: tool signatures are turned into JSON Schema so the host can advertise them.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import sys
import typing
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

__minimcp_version__ = "1.0.0"

logger = logging.getLogger("minimcp")

# --------------------------------------------------------------------------- protocol

#: Protocol revisions this server understands, newest first. The client proposes
#: one in ``initialize``; if we do not know it we answer with our preferred one
#: and let the client decide whether to continue, as the specification requires.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
PREFERRED_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 reserved error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: HTTP header carrying the session identifier issued at ``initialize``.
SESSION_HEADER = "Mcp-Session-Id"
#: HTTP header carrying the negotiated protocol revision.
PROTOCOL_HEADER = "MCP-Protocol-Version"


class ToolError(Exception):
    """A tool failure the author anticipated.

    Reported inside the result as ``isError: true`` rather than as a JSON-RPC
    error, so the model can read the message and correct itself. That distinction
    is part of the specification: protocol errors are for the *protocol*, tool
    failures are data.
    """


# --------------------------------------------------------------------------- messages


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    """A JSON-RPC success response: has ``id`` and ``result``, never ``error``."""
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """A JSON-RPC error response: has ``id`` and ``error``, never ``result``."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": payload}


def classify(message: dict[str, Any]) -> str:
    """Name the JSON-RPC message kind. Used by the tests and by the wire log.

    The four kinds the specification defines, and the only thing you need in
    order to read a capture:

    * ``request``      - has ``method`` **and** ``id``; expects exactly one response.
    * ``notification`` - has ``method`` and **no** ``id``; must never be answered.
    * ``response``     - has ``id`` and ``result``.
    * ``error``        - has ``id`` and ``error``.
    """
    if "method" in message:
        return "request" if "id" in message else "notification"
    if "error" in message:
        return "error"
    if "result" in message:
        return "response"
    return "unknown"


# ------------------------------------------------------------------------------ tools


@dataclass
class Tool:
    """One registered tool: the callable plus everything ``tools/list`` publishes."""

    name: str
    description: str
    handler: Callable[..., Any]
    arguments_model: type[BaseModel]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    title: str | None = None
    returns_model: bool = False

    def descriptor(self) -> dict[str, Any]:
        """The object that goes into the ``tools/list`` result."""
        entry: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title:
            entry["title"] = self.title
        if self.output_schema is not None:
            entry["outputSchema"] = self.output_schema
        return entry


def _arguments_model(fn: Callable[..., Any]) -> type[BaseModel]:
    """Build a pydantic model from a function signature.

    The model does two jobs: it produces the JSON Schema advertised in
    ``tools/list``, and it validates the arguments of every ``tools/call`` before
    the tool body runs.
    """
    signature = inspect.signature(fn)
    hints = typing.get_type_hints(fn, include_extras=True)
    fields: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if name in ("self", "cls"):
            continue
        annotation = hints.get(name, Any)
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[name] = (annotation, default)
    # `extra="forbid"` rejects unexpected keys instead of ignoring them: a model
    # that invents an argument should be told, not silently obeyed with a default.
    # It has to be passed to create_model -- assigning to model_config afterwards
    # does not rebuild the validator.
    return create_model(f"{fn.__name__}_arguments", __config__=ConfigDict(extra="forbid"), **fields)


def _output_schema(fn: Callable[..., Any]) -> tuple[dict[str, Any] | None, bool]:
    """Derive the output schema from the return annotation.

    Returns ``(schema, returns_model)``. A ``BaseModel`` return type publishes its
    own schema; any other concrete type is wrapped as ``{"result": <value>}``,
    which is the convention the reference implementation also uses, so hosts see
    one predictable shape.
    """
    hints = typing.get_type_hints(fn, include_extras=True)
    annotation = hints.get("return")
    if annotation is None or annotation is type(None):
        return None, False
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation.model_json_schema(), True
    try:
        inner = TypeAdapter(annotation).json_schema()
    except Exception:  # noqa: BLE001 - an exotic annotation simply gets no schema
        return None, False
    return {
        "type": "object",
        "properties": {"result": inner},
        "required": ["result"],
        "title": f"{fn.__name__}_result",
    }, False


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


# ----------------------------------------------------------------------------- server


@dataclass
class MCPServer:
    """A Model Context Protocol server.

    Register tools with :meth:`tool`, then run it with :func:`run_stdio` or
    :func:`run_http`. The class itself is transport-agnostic: it turns one decoded
    JSON-RPC message into zero or one reply, and knows nothing about pipes or
    sockets.
    """

    name: str
    version: str = "0.0.0"
    title: str | None = None
    instructions: str | None = None
    tools: dict[str, Tool] = field(default_factory=dict)
    #: Custom plain-HTTP routes, e.g. a health check. Never exposed as tools.
    http_routes: list[tuple[str, list[str], Callable[..., Awaitable[Any]]]] = field(default_factory=list)
    #: Set once the client has completed the handshake.
    initialized: bool = False
    negotiated_version: str = PREFERRED_PROTOCOL_VERSION

    # ------------------------------------------------------------- registration

    def tool(
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a function as a tool.

        The JSON Schema is derived from the type hints, so the signature is the
        single source of truth for what the model may send.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            if tool_name in self.tools:
                raise ValueError(f"Tool {tool_name!r} is already registered.")
            model = _arguments_model(fn)
            schema = model.model_json_schema()
            schema.pop("title", None)
            output_schema, returns_model = _output_schema(fn)
            self.tools[tool_name] = Tool(
                name=tool_name,
                description=(description or inspect.getdoc(fn) or "").strip(),
                handler=fn,
                arguments_model=model,
                input_schema=schema,
                output_schema=output_schema,
                title=title,
                returns_model=returns_model,
            )
            return fn

        return decorator

    def route(
        self, path: str, methods: Iterable[str] = ("GET",)
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """Register a plain HTTP route (health checks and the like).

        Deliberately separate from :meth:`tool`: a deployment probe is not
        something a model should be able to call, and it must not appear in
        ``tools/list``.
        """

        def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            self.http_routes.append((path, list(methods), fn))
            return fn

        return decorator

    # ------------------------------------------------------------- lifecycle

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Negotiate the protocol revision and advertise what this server can do."""
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            self.negotiated_version = requested
        else:
            # The specification says to answer with a version we do support and
            # let the client decide whether to proceed.
            self.negotiated_version = PREFERRED_PROTOCOL_VERSION
            logger.info(
                "Client asked for protocol %r; answering with %r",
                requested,
                self.negotiated_version,
            )
        result: dict[str, Any] = {
            "protocolVersion": self.negotiated_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.title:
            result["serverInfo"]["title"] = self.title
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    # ------------------------------------------------------------- tool calls

    async def _invoke(self, tool: Tool, arguments: dict[str, Any]) -> Any:
        outcome = tool.handler(**arguments)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one tool and shape the result the way the specification defines."""
        tool_name = params.get("name")
        raw_arguments = params.get("arguments") or {}
        tool = self.tools.get(tool_name or "")
        if tool is None:
            return {
                "content": [
                    _text_block(
                        f"Unknown tool {tool_name!r}. Available tools: "
                        f"{', '.join(sorted(self.tools)) or '(none)'}."
                    )
                ],
                "isError": True,
            }

        try:
            validated = tool.arguments_model(**raw_arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors()
            )
            return {
                "content": [_text_block(f"Invalid arguments for {tool_name}: {problems}")],
                "isError": True,
            }
        except TypeError as exc:  # pragma: no cover - pydantic normally raises above
            return {"content": [_text_block(f"Invalid arguments: {exc}")], "isError": True}

        try:
            outcome = await self._invoke(tool, validated.model_dump())
        except ToolError as exc:
            logger.info("Tool %r failed: %s", tool_name, exc)
            return {"content": [_text_block(str(exc))], "isError": True}
        except Exception:  # noqa: BLE001 - a crash must not take the server down
            logger.exception("Tool %r raised an unexpected exception", tool_name)
            return {
                "content": [_text_block(f"Error executing tool {tool_name}")],
                "isError": True,
            }

        return self._shape_result(tool, outcome)

    @staticmethod
    def _shape_result(tool: Tool, outcome: Any) -> dict[str, Any]:
        """Turn a tool's return value into a ``CallToolResult``.

        A tool that publishes an output schema also returns ``structuredContent``,
        so a host can consume the data without parsing prose. The text block is
        kept as well, for hosts and models that only read text.
        """
        if isinstance(outcome, BaseModel):
            structured = outcome.model_dump(mode="json")
        elif tool.output_schema is not None and not tool.returns_model:
            structured = {"result": TypeAdapter(type(outcome)).dump_python(outcome, mode="json")}
        else:
            structured = None

        if structured is not None:
            text = json.dumps(structured, ensure_ascii=False, default=str)
            return {"content": [_text_block(text)], "structuredContent": structured, "isError": False}
        if isinstance(outcome, str):
            return {"content": [_text_block(outcome)], "isError": False}
        return {
            "content": [_text_block(json.dumps(outcome, ensure_ascii=False, default=str))],
            "isError": False,
        }

    # ------------------------------------------------------------- dispatch

    async def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC message; return the reply, or ``None``.

        ``None`` means "send nothing", which is the only correct answer to a
        notification.
        """
        kind = classify(message)
        if kind == "notification":
            method = message.get("method")
            if method == "notifications/initialized":
                self.initialized = True
                logger.info("Client completed the handshake.")
            elif method == "notifications/cancelled":
                logger.info("Client cancelled a request; nothing to undo.")
            else:
                logger.info("Ignoring unknown notification %r", method)
            return None

        if kind != "request":
            # A server receiving a response it never asked for: log and drop.
            logger.info("Ignoring unexpected %s message.", kind)
            return None

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")

        try:
            if method == "initialize":
                return _response(request_id, self._handle_initialize(params))
            if method == "ping":
                return _response(request_id, {})
            if method == "tools/list":
                return _response(request_id, {"tools": [tool.descriptor() for tool in self.tools.values()]})
            if method == "tools/call":
                return _response(request_id, await self._handle_tools_call(params))
        except Exception as exc:  # noqa: BLE001 - never drop a request without replying
            logger.exception("Internal error handling %r", method)
            return _error(request_id, INTERNAL_ERROR, f"Internal error: {exc}")

        return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    async def handle_raw(self, raw: str) -> str | None:
        """Decode one frame, dispatch it, and encode the reply."""
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            return json.dumps(_error(None, PARSE_ERROR, f"Parse error: {exc}"))
        if not isinstance(message, dict):
            return json.dumps(_error(None, INVALID_REQUEST, "Batch requests are not supported"))
        reply = await self.handle_message(message)
        return None if reply is None else json.dumps(reply, ensure_ascii=False, default=str)


# -------------------------------------------------------------------------- transports


async def serve_stdio(server: MCPServer) -> None:
    """Serve on stdin/stdout with newline-delimited JSON framing.

    Nothing but protocol frames may ever reach stdout, so every diagnostic goes to
    stderr. ``sys.stdin.readline`` runs in a worker thread because a portable
    asyncio reader for stdin does not exist on Windows.
    """
    loop_input = sys.stdin
    output = sys.stdout
    while True:
        line = await asyncio.to_thread(loop_input.readline)
        if line == "":  # EOF: the host closed the pipe, so we are done
            logger.info("stdin closed; shutting down.")
            return
        line = line.strip()
        if not line:
            continue
        reply = await server.handle_raw(line)
        if reply is not None:
            output.write(reply + "\n")
            output.flush()


def run_stdio(server: MCPServer) -> None:
    """Blocking entry point for the stdio transport."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    # Ctrl-C is how a host stops an interactive run; it is not an error.
    with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover - interactive only
        asyncio.run(serve_stdio(server))


def build_http_app(server: MCPServer, mcp_path: str = "/mcp") -> Any:
    """Build a Starlette application exposing the Streamable HTTP transport.

    Starlette and uvicorn are a web framework and an HTTP server, not MCP
    libraries: the MCP layer above is still hand-written.

    Requests answer with ``application/json``. The specification also allows
    ``text/event-stream``, but a plain JSON body keeps one request and one
    response in one TCP exchange, which is far easier to read in a packet
    capture.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    async def mcp_endpoint(request: Request) -> Response:
        if request.method == "DELETE":
            # Session termination. Nothing is stored per session, so this is an
            # acknowledgement, but answering it keeps well-behaved clients happy.
            logger.info("Session %s terminated.", request.headers.get(SESSION_HEADER, "-"))
            return Response(status_code=204)

        if request.method == "GET":
            # The optional server-to-client stream. This server never initiates a
            # message, so it declines rather than holding a socket open.
            return Response(status_code=405, headers={"Allow": "POST, DELETE"})

        body = await request.body()
        reply = await server.handle_raw(body.decode("utf-8"))
        if reply is None:
            # A notification. HTTP still needs a status: 202 says "accepted, no
            # content", which is exactly what a JSON-RPC notification means.
            return Response(status_code=202)

        headers = {PROTOCOL_HEADER: server.negotiated_version}
        try:
            decoded = json.loads(reply)
        except json.JSONDecodeError:  # pragma: no cover - we just encoded it
            decoded = None
        if (
            isinstance(decoded, dict)
            and "result" in decoded
            and "protocolVersion" in (decoded.get("result") or {})
        ):
            # Issue the session id on the initialize response, as the transport
            # specification describes; the client echoes it on later requests.
            headers[SESSION_HEADER] = uuid.uuid4().hex
        return Response(reply, media_type="application/json", headers=headers)

    routes = [Route(mcp_path, mcp_endpoint, methods=["POST", "GET", "DELETE"])]
    for path, methods, handler in server.http_routes:
        routes.append(Route(path, handler, methods=methods))
    return Starlette(routes=routes)


def run_http(
    server: MCPServer, host: str = "127.0.0.1", port: int = 8080, mcp_path: str = "/mcp"
) -> None:  # pragma: no cover - exercised by the deployed service and by hand
    """Blocking entry point for the Streamable HTTP transport."""
    import uvicorn

    uvicorn.run(build_http_app(server, mcp_path), host=host, port=port, log_level="info")


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "MCPServer",
    "Tool",
    "ToolError",
    "PREFERRED_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SESSION_HEADER",
    "PROTOCOL_HEADER",
    "classify",
    "build_http_app",
    "run_http",
    "run_stdio",
    "serve_stdio",
]
