"""Value objects shared by the MCP host.

The package is called ``mcp_host`` rather than ``mcp`` so that reading
``from mcp.client import Client`` inside this project is never ambiguous about
which ``mcp`` is meant: the official SDK, always.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Transport = Literal["stdio", "streamable-http"]

#: Separator used to qualify a tool name when two servers expose the same one.
#: A dot is not allowed by the Claude API tool-name pattern, so a double
#: underscore is used instead.
QUALIFIER = "__"


@dataclass(frozen=True)
class ServerConfig:
    """One entry of ``config/servers.toml``.

    Attributes:
        name: Stable identifier used in logs, in ``/servers`` and to qualify tool names.
        transport: ``stdio`` (the host spawns a subprocess) or ``streamable-http``
            (the host opens an HTTP connection to a running server).
        enabled: Disabled servers are listed by ``/servers`` but never connected.
        description: Free text shown by ``/servers``.
        command: Executable for a stdio server.
        args: Arguments for a stdio server.
        env: Extra environment variables merged over the inherited environment.
        cwd: Working directory for a stdio server.
        url: Endpoint of a Streamable HTTP server, usually ending in ``/mcp``.
        timeout_seconds: Per-``tools/call`` timeout enforced by the host.
        connect_timeout_seconds: Ceiling on one connection attempt, so a server
            that never answers the negotiation cannot stall start-up.
    """

    name: str
    transport: Transport
    enabled: bool = True
    description: str = ""
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    timeout_seconds: float = 60.0
    connect_timeout_seconds: float = 30.0

    def validate(self, config_dir: Path) -> None:
        """Raise ``ValueError`` with an actionable message if the entry is unusable."""
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(
                f"Server name {self.name!r} must be alphanumeric (underscores and dashes allowed)."
            )
        if self.transport == "stdio":
            if not self.command:
                raise ValueError(f"Server '{self.name}': transport 'stdio' requires a 'command'.")
            if self.url:
                raise ValueError(f"Server '{self.name}': 'url' is meaningless for transport 'stdio'.")
            if self.cwd and not (config_dir / self.cwd).resolve().is_dir():
                raise ValueError(
                    f"Server '{self.name}': cwd '{self.cwd}' does not exist (resolved from {config_dir})."
                )
        elif self.transport == "streamable-http":
            if not self.url:
                raise ValueError(f"Server '{self.name}': transport 'streamable-http' requires a 'url'.")
            if not self.url.startswith(("http://", "https://")):
                raise ValueError(f"Server '{self.name}': url must start with http:// or https://.")
            if self.command:
                raise ValueError(
                    f"Server '{self.name}': 'command' is meaningless for transport 'streamable-http'."
                )
        else:  # pragma: no cover - guarded by the TOML reader
            raise ValueError(f"Server '{self.name}': unknown transport {self.transport!r}.")
        if self.timeout_seconds <= 0:
            raise ValueError(f"Server '{self.name}': timeout_seconds must be positive.")
        if self.connect_timeout_seconds <= 0:
            raise ValueError(f"Server '{self.name}': connect_timeout_seconds must be positive.")

    @property
    def target(self) -> str:
        """A short human-readable description of where this server lives."""
        if self.transport == "stdio":
            return " ".join([self.command or "", *self.args]).strip()
        return self.url or ""


@dataclass(frozen=True)
class ToolRef:
    """A tool discovered on a server, plus the name the LLM sees.

    ``exposed_name`` equals ``original_name`` unless two servers published the same
    tool, in which case every colliding tool is qualified as
    ``<server>__<original_name>``.
    """

    server: str
    original_name: str
    exposed_name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def is_qualified(self) -> bool:
        return self.exposed_name != self.original_name


@dataclass
class ServerStatus:
    """Live state of one configured server, for ``/servers``."""

    config: ServerConfig
    connected: bool = False
    tool_count: int = 0
    error: str | None = None
    server_title: str | None = None
    protocol_version: str | None = None
    #: Usage guidance the server sent in its ``initialize`` result, if any.
    instructions: str | None = None
    discovered_tools: list[Any] = field(default_factory=list)

    @property
    def state(self) -> str:
        if not self.config.enabled:
            return "disabled"
        if self.connected:
            return "connected"
        return "failed" if self.error else "not connected"


@dataclass
class ToolCallOutcome:
    """The result of routing one tool call through the manager.

    ``text`` is what gets handed back to the LLM; ``structured`` is kept for the
    presentation layer and for the log.
    """

    tool: str
    server: str
    ok: bool
    text: str
    structured: Any = None
    elapsed_ms: int = 0
    request_id: str = ""
