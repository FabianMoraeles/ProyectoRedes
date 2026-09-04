"""pet-care-mcp: generic pet care guidance served over Streamable HTTP."""

from pet_care_mcp.server import __version__, server

__all__ = ["server", "__version__"]
