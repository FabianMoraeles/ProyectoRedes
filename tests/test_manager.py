"""Discovery, routing, name collisions, timeouts and clean shutdown.

Every server in this module is a real subprocess speaking JSON-RPC over stdio.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from adoptamatch_chatbot.mcp_host.logger import InteractionLogger
from adoptamatch_chatbot.mcp_host.manager import MCPManager, UnknownToolError
from adoptamatch_chatbot.mcp_host.models import ServerConfig
from tests.conftest import fixture_server

# ------------------------------------------------------------------- discovery


async def test_tools_are_discovered_from_a_connected_server(alpha_manager: MCPManager) -> None:
    names = {reference.exposed_name for reference in alpha_manager.tools}
    assert names == {"ping", "add", "boom"}
    assert alpha_manager.connected_servers == ["alpha"]


async def test_status_records_the_handshake_details(alpha_manager: MCPManager) -> None:
    status = alpha_manager.statuses[0]
    assert status.state == "connected"
    assert status.tool_count == 3
    assert status.protocol_version
    assert status.negotiated_mode == "legacy"
    assert status.error is None


async def test_tools_from_several_servers_are_merged(duo_manager: MCPManager) -> None:
    servers = {reference.server for reference in duo_manager.tools}
    assert servers == {"alpha", "beta"}
    assert len(duo_manager.tools) == 5  # 3 from alpha + 2 from beta


# ------------------------------------------------------------- name collisions


async def test_colliding_names_are_qualified_and_unique_names_are_not(duo_manager: MCPManager) -> None:
    exposed = {reference.exposed_name for reference in duo_manager.tools}
    assert "alpha__ping" in exposed and "beta__ping" in exposed
    assert "ping" not in exposed  # the bare name would be ambiguous
    assert "add" in exposed and "shout" in exposed  # unique names stay bare


async def test_a_qualified_call_reaches_the_right_server(duo_manager: MCPManager) -> None:
    alpha = await duo_manager.call_tool("alpha__ping", {"name": "Ana"})
    beta = await duo_manager.call_tool("beta__ping", {"name": "Ana"})
    assert alpha.server == "alpha" and "alpha says hello to Ana" in alpha.text
    assert beta.server == "beta" and "beta says hello to Ana" in beta.text


async def test_unqualified_call_reaches_its_only_owner(duo_manager: MCPManager) -> None:
    outcome = await duo_manager.call_tool("shout", {"text": "quiet"})
    assert outcome.server == "beta"
    assert "QUIET" in outcome.text


async def test_unknown_tool_raises_with_the_available_list(duo_manager: MCPManager) -> None:
    with pytest.raises(UnknownToolError, match="alpha__ping"):
        await duo_manager.call_tool("does_not_exist", {})


# ------------------------------------------------------------------ tool calls


async def test_successful_call_reports_timing_and_correlation(alpha_manager: MCPManager) -> None:
    outcome = await alpha_manager.call_tool("add", {"a": 2, "b": 3})
    assert outcome.ok is True
    assert outcome.structured == {"result": 5}
    assert outcome.elapsed_ms >= 0
    assert len(outcome.request_id) == 12


async def test_tool_error_is_returned_not_raised(alpha_manager: MCPManager) -> None:
    """The model must see the failure as a result it can react to."""
    outcome = await alpha_manager.call_tool("boom", {})
    assert outcome.ok is False
    assert "refused on purpose" in outcome.text


async def test_invalid_arguments_are_reported_as_a_failed_result(alpha_manager: MCPManager) -> None:
    outcome = await alpha_manager.call_tool("add", {"a": "not-a-number", "b": 3})
    assert outcome.ok is False


# ------------------------------------------------------- failures and timeouts


async def test_a_server_that_cannot_start_is_isolated(
    interaction_log: InteractionLogger, tmp_path: Path
) -> None:
    """One broken entry must not stop the others from working."""
    broken = ServerConfig(
        name="broken",
        transport="stdio",
        command=sys.executable,
        args=["-c", "raise SystemExit(1)"],
        mode="legacy",
        connect_timeout_seconds=20.0,
    )
    manager = MCPManager([broken, fixture_server("alpha", "alpha_server.py")], interaction_log, tmp_path)
    await manager.connect_all()
    try:
        states = {status.config.name: status.state for status in manager.statuses}
        assert states["broken"] == "failed"
        assert states["alpha"] == "connected"
        outcome = await manager.call_tool("add", {"a": 1, "b": 1})
        assert outcome.ok is True
    finally:
        await manager.aclose()


async def test_a_slow_tool_times_out_without_killing_the_session(
    interaction_log: InteractionLogger, tmp_path: Path
) -> None:
    manager = MCPManager(
        [fixture_server("slow", "slow_server.py", timeout_seconds=1.0)], interaction_log, tmp_path
    )
    await manager.connect_all()
    try:
        outcome = await manager.call_tool("nap", {"seconds": 20})
        assert outcome.ok is False
        assert "failed" in outcome.text.lower()
    finally:
        await manager.aclose()


async def test_disabled_servers_are_listed_but_never_connected(
    interaction_log: InteractionLogger, tmp_path: Path
) -> None:
    manager = MCPManager(
        [fixture_server("alpha", "alpha_server.py", enabled=False)], interaction_log, tmp_path
    )
    await manager.connect_all()
    try:
        assert manager.statuses[0].state == "disabled"
        assert manager.tools == []
        assert manager.connected_servers == []
    finally:
        await manager.aclose()


# ------------------------------------------------------------------- shutdown


async def test_aclose_releases_everything_and_is_idempotent(
    interaction_log: InteractionLogger, tmp_path: Path
) -> None:
    manager = MCPManager([fixture_server("alpha", "alpha_server.py")], interaction_log, tmp_path)
    await manager.connect_all()
    assert manager.connected_servers == ["alpha"]

    await manager.aclose()
    assert manager.connected_servers == []
    assert manager.statuses[0].connected is False

    await manager.aclose()  # a second call must not raise
    assert manager.connected_servers == []
