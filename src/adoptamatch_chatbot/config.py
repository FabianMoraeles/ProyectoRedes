"""Configuration loading and validation.

Two sources, deliberately separated:

* **Secrets and runtime knobs** come from the environment (optionally seeded from
  a ``.env`` file). Nothing secret is ever read from a file that is under version
  control.
* **The MCP server inventory** comes from a declarative TOML file. Adding a
  server is an edit to that file, never a code change.

Every failure raised here is meant to be printed straight to the user, so each
message says what is wrong *and* what to do about it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from adoptamatch_chatbot.mcp_host.models import ServerConfig

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
DEFAULT_PROVIDER = "anthropic"
SUPPORTED_PROVIDERS = ("anthropic", "gemini")
DEFAULT_SERVERS_CONFIG = "config/servers.toml"
DEFAULT_LOG_DIR = "logs"


class ConfigError(RuntimeError):
    """A configuration problem the user can fix. Printed without a traceback."""


@dataclass
class AppConfig:
    """Everything the chatbot needs to start."""

    provider: str
    anthropic_api_key: str
    gemini_api_key: str
    model: str
    servers_config_path: Path
    log_dir: Path
    log_level: str
    max_tool_iterations: int
    servers: list[ServerConfig] = field(default_factory=list)

    @property
    def config_dir(self) -> Path:
        """Directory of ``servers.toml``; relative server paths resolve against it."""
        return self.servers_config_path.parent


def load_server_configs(path: Path) -> list[ServerConfig]:
    """Parse and validate ``servers.toml``.

    Expected shape::

        [[servers]]
        name = "adoptamatch"
        transport = "stdio"
        command = "uv"
        args = ["--directory", "../adoptamatch-mcp", "run", "adoptamatch-mcp"]
    """
    if not path.is_file():
        raise ConfigError(
            f"MCP server configuration not found at '{path}'. "
            "Copy config/servers.example.toml to config/servers.toml and edit the paths."
        )
    try:
        with path.open("rb") as handle:
            document: dict[str, Any] = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"'{path}' is not valid TOML: {exc}") from exc

    entries = document.get("servers")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"'{path}' must define at least one [[servers]] entry.")

    known_fields = set(ServerConfig.__dataclass_fields__)
    configs: list[ServerConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"[[servers]] entry #{index} in '{path}' is not a table.")
        unknown = set(entry) - known_fields
        if unknown:
            raise ConfigError(
                f"[[servers]] entry #{index} in '{path}' has unknown key(s): "
                f"{', '.join(sorted(unknown))}. Valid keys: {', '.join(sorted(known_fields))}."
            )
        try:
            config = ServerConfig(**entry)
        except TypeError as exc:
            raise ConfigError(f"[[servers]] entry #{index} in '{path}': {exc}") from exc
        if config.name in seen:
            raise ConfigError(f"Duplicate server name '{config.name}' in '{path}'.")
        seen.add(config.name)
        try:
            config.validate(path.parent)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        configs.append(config)
    return configs


def load_config(
    *,
    env_file: Path | None = None,
    servers_config: Path | None = None,
    require_api_key: bool = True,
) -> AppConfig:
    """Load the environment and the server inventory, or fail with a clear message.

    Args:
        env_file: ``.env`` to load before reading the environment. Values already
            present in the real environment win.
        servers_config: Overrides ``MCP_SERVERS_CONFIG``.
        require_api_key: Set to ``False`` for offline runs that use a stub provider.
    """
    if env_file is None:
        env_file = Path(".env")
    if env_file.is_file():
        load_dotenv(env_file, override=False)

    provider = os.environ.get("LLM_PROVIDER", "").strip().lower() or DEFAULT_PROVIDER
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER must be one of {', '.join(SUPPORTED_PROVIDERS)}, got '{provider}'.")

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if require_api_key:
        if provider == "anthropic" and not anthropic_api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key, "
                "set LLM_PROVIDER=gemini for a free alternative, or run with --offline."
            )
        if provider == "gemini" and not gemini_api_key:
            raise ConfigError(
                "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey, "
                "add it to .env, or run with --offline."
            )

    if provider == "gemini":
        model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    else:
        model = os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL
    path = servers_config or Path(os.environ.get("MCP_SERVERS_CONFIG", DEFAULT_SERVERS_CONFIG))
    log_dir = Path(os.environ.get("LOG_DIR", DEFAULT_LOG_DIR))
    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

    raw_iterations = os.environ.get("MAX_TOOL_ITERATIONS", "8").strip()
    try:
        max_tool_iterations = int(raw_iterations)
    except ValueError as exc:
        raise ConfigError(f"MAX_TOOL_ITERATIONS must be an integer, got '{raw_iterations}'.") from exc
    if max_tool_iterations < 1:
        raise ConfigError("MAX_TOOL_ITERATIONS must be at least 1.")

    return AppConfig(
        provider=provider,
        anthropic_api_key=anthropic_api_key,
        gemini_api_key=gemini_api_key,
        model=model,
        servers_config_path=path.resolve(),
        log_dir=log_dir,
        log_level=log_level,
        max_tool_iterations=max_tool_iterations,
        servers=load_server_configs(path),
    )
