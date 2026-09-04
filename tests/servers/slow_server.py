"""Stdio MCP server whose only tool sleeps longer than any sane timeout."""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="slow", version="1.0.0")


@server.tool(description="Sleep for the requested number of seconds, then answer.")
async def nap(seconds: float = 30.0) -> str:
    await asyncio.sleep(seconds)
    return "finally awake"


if __name__ == "__main__":
    server.run(transport="stdio")
