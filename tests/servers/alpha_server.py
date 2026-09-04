"""Tiny stdio MCP server used by the host tests.

Exposes ``ping`` (which ``beta_server`` also exposes, to force a name collision),
``add`` and ``boom`` (which always fails, to exercise error propagation).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer(name="alpha", version="1.0.0", instructions="Test server alpha.")


@server.tool(description="Return a greeting from the alpha server.")
def ping(name: str = "world") -> str:
    return f"alpha says hello to {name}"


@server.tool(description="Add two integers.")
def add(a: int, b: int) -> int:
    return a + b


@server.tool(description="Always fails, on purpose.")
def boom() -> str:
    raise ToolError("alpha refused on purpose")


if __name__ == "__main__":
    server.run(transport="stdio")
