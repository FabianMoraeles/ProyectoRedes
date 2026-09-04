"""pet-care-mcp: generic pet care guidance served over Streamable HTTP.

The MCP protocol is implemented directly over JSON-RPC in :mod:`pet_care_mcp.minimcp`;
no MCP SDK is used at runtime.
"""

from pet_care_mcp.server import __version__, server

__all__ = ["server", "__version__"]
