"""Shared fixtures for the chatbot tests.

No test here ever contacts a language-model API: every LLM turn is produced by
:class:`~adoptamatch_chatbot.llm.scripted.ScriptedProvider`. The MCP side, in
contrast, is real -- the fixture servers under ``tests/servers/`` are launched as
actual subprocesses and spoken to over stdio.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from adoptamatch_chatbot.mcp_host.logger import InteractionLogger
from adoptamatch_chatbot.mcp_host.manager import MCPManager
from adoptamatch_chatbot.mcp_host.models import ServerConfig

SERVERS_DIR = Path(__file__).parent / "servers"


def fixture_server(name: str, script: str, **overrides: object) -> ServerConfig:
    """A ``ServerConfig`` that launches one of the fixture servers."""
    defaults: dict[str, object] = {
        "name": name,
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(SERVERS_DIR / script)],
        "mode": "legacy",  # the fixture servers are ours; skip the discover probe
        "timeout_seconds": 15.0,
        "connect_timeout_seconds": 30.0,
    }
    defaults.update(overrides)
    return ServerConfig(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def interaction_log(tmp_path: Path) -> InteractionLogger:
    return InteractionLogger(tmp_path / "logs")


@pytest.fixture
async def alpha_manager(interaction_log: InteractionLogger, tmp_path: Path) -> AsyncIterator[MCPManager]:
    """One connected server."""
    manager = MCPManager([fixture_server("alpha", "alpha_server.py")], interaction_log, tmp_path)
    await manager.connect_all()
    try:
        yield manager
    finally:
        await manager.aclose()


@pytest.fixture
async def duo_manager(interaction_log: InteractionLogger, tmp_path: Path) -> AsyncIterator[MCPManager]:
    """Two connected servers that both expose a tool called ``ping``."""
    manager = MCPManager(
        [
            fixture_server("alpha", "alpha_server.py"),
            fixture_server("beta", "beta_server.py"),
        ],
        interaction_log,
        tmp_path,
    )
    await manager.connect_all()
    try:
        yield manager
    finally:
        await manager.aclose()
