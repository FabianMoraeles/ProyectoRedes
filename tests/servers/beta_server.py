"""Second stdio MCP server for the host tests.

Deliberately exposes a tool called ``ping`` as well, so the manager has to
qualify both as ``alpha__ping`` / ``beta__ping``.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="beta", version="1.0.0", instructions="Test server beta.")


@server.tool(description="Return a greeting from the beta server.")
def ping(name: str = "world") -> str:
    return f"beta says hello to {name}"


@server.tool(description="Shout the given text.")
def shout(text: str) -> str:
    return text.upper()


if __name__ == "__main__":
    server.run(transport="stdio")
