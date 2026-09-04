"""Tests for pet-care-mcp, including a real Streamable HTTP round-trip.

The HTTP test starts uvicorn on an ephemeral port in a background thread and then
connects a real :class:`mcp.client.Client` to ``http://127.0.0.1:<port>/mcp``. That
is the same code path the chatbot uses against the deployed server, and the same
one Wireshark observes.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx2
import pytest
import uvicorn
from mcp.client import Client

from pet_care_mcp.server import server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def http_endpoint() -> Iterator[str]:
    """Serve the app on a real TCP socket for the duration of the module."""
    port = _free_port()
    app = server.streamable_http_app(streamable_http_path="/mcp")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not running.started:
        if time.time() > deadline:  # pragma: no cover - CI safety net
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.05)

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        running.should_exit = True
        thread.join(timeout=10)


# ------------------------------------------------------------------ pure logic


async def test_tools_are_discoverable_in_process() -> None:
    async with Client(server) as client:
        listed = await client.list_tools()
    names = {tool.name for tool in listed.tools}
    assert names == {"get_daily_care_checklist", "estimate_daily_water_ml"}


async def test_health_route_is_not_an_mcp_tool() -> None:
    """A deployment probe must not be callable by the model."""
    async with Client(server) as client:
        listed = await client.list_tools()
    assert "healthz" not in {tool.name for tool in listed.tools}


async def test_checklist_scales_with_life_stage() -> None:
    async with Client(server) as client:
        adult = await client.call_tool(
            "get_daily_care_checklist",
            {"species": "dog", "life_stage": "adult", "energy_level": "high"},
        )
        senior = await client.call_tool(
            "get_daily_care_checklist",
            {"species": "dog", "life_stage": "senior", "energy_level": "high"},
        )
    assert (
        senior.structured_content["total_active_minutes"] < adult.structured_content["total_active_minutes"]
    )
    assert adult.structured_content["meals_per_day"] == 2
    assert "not veterinary advice" in adult.structured_content["disclaimer"]


async def test_water_estimate_is_proportional_and_bounded() -> None:
    async with Client(server) as client:
        small = await client.call_tool("estimate_daily_water_ml", {"species": "dog", "weight_kg": 5})
        large = await client.call_tool("estimate_daily_water_ml", {"species": "dog", "weight_kg": 30})
    assert small.structured_content["estimated_ml_per_day"] < large.structured_content["estimated_ml_per_day"]
    low, high = large.structured_content["range_ml_per_day"]
    assert low < large.structured_content["estimated_ml_per_day"] < high


@pytest.mark.parametrize("weight", [0, -3, 500])
async def test_invalid_weight_is_rejected(weight: float) -> None:
    async with Client(server) as client:
        result = await client.call_tool("estimate_daily_water_ml", {"species": "dog", "weight_kg": weight})
    assert result.is_error is True


async def test_unknown_species_is_rejected() -> None:
    async with Client(server) as client:
        result = await client.call_tool("estimate_daily_water_ml", {"species": "iguana", "weight_kg": 2})
    assert result.is_error is True


# ------------------------------------------------------- real Streamable HTTP


async def test_health_endpoint_over_http(http_endpoint: str) -> None:
    async with httpx2.AsyncClient() as http:
        response = await http.get(f"{http_endpoint}/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_streamable_http_round_trip(http_endpoint: str, mode: str) -> None:
    """The full path the chatbot uses: connect, tools/list, tools/call over TCP."""
    async with Client(f"{http_endpoint}/mcp", mode=mode) as client:  # type: ignore[arg-type]
        listed = await client.list_tools()
        assert "get_daily_care_checklist" in {tool.name for tool in listed.tools}
        result = await client.call_tool(
            "get_daily_care_checklist",
            {"species": "cat", "life_stage": "adult", "energy_level": "medium"},
        )
    assert result.is_error is False
    assert result.structured_content["species"] == "cat"
    assert result.structured_content["items"]
