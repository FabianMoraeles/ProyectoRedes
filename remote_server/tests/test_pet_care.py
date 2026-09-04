"""Tests for pet-care-mcp at three levels of fidelity.

1. ``TestJsonRpc`` builds JSON-RPC messages by hand and checks the protocol.
2. ``TestStreamableHttp`` starts uvicorn on a real TCP socket and speaks HTTP to
   it, which is exactly what the chatbot and Wireshark see.
3. ``TestReferenceInterop`` points the **official MCP SDK client** at this
   hand-written server. The SDK is a test-only dependency, used as a conformance
   oracle; nothing in the shipped package imports it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx2
import pytest
import uvicorn

from pet_care_mcp.minimcp import (
    METHOD_NOT_FOUND,
    PREFERRED_PROTOCOL_VERSION,
    SESSION_HEADER,
    build_http_app,
    classify,
)
from pet_care_mcp.server import server

TOOL_NAMES = {"get_daily_care_checklist", "estimate_daily_water_ml"}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PREFERRED_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def http_endpoint() -> Iterator[str]:
    """Serve the app on a real TCP socket for the duration of the module."""
    port = _free_port()
    config = uvicorn.Config(build_http_app(server), host="127.0.0.1", port=port, log_level="warning")
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


# ------------------------------------------------------------------ 1. raw JSON-RPC


class TestJsonRpc:
    async def test_initialize_advertises_the_server(self) -> None:
        reply = await server.handle_message(INITIALIZE)
        result = reply["result"]
        assert result["protocolVersion"] == PREFERRED_PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "pet-care"
        assert "veterinarian" in result["instructions"]

    async def test_tools_list_exposes_exactly_two_tools(self) -> None:
        reply = await server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = reply["result"]["tools"]
        assert {tool["name"] for tool in tools} == TOOL_NAMES
        for tool in tools:
            assert tool["inputSchema"]["type"] == "object"
            assert "outputSchema" in tool

    async def test_health_route_is_not_an_mcp_tool(self) -> None:
        """A deployment probe must not be callable by the model."""
        reply = await server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert "healthz" not in {tool["name"] for tool in reply["result"]["tools"]}

    async def test_unknown_method_is_method_not_found(self) -> None:
        reply = await server.handle_message({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        assert reply["error"]["code"] == METHOD_NOT_FOUND
        assert classify(reply) == "error"

    async def test_checklist_scales_with_life_stage(self) -> None:
        async def checklist(life_stage: str) -> dict:
            reply = await server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "get_daily_care_checklist",
                        "arguments": {
                            "species": "dog",
                            "life_stage": life_stage,
                            "energy_level": "high",
                        },
                    },
                }
            )
            return reply["result"]["structuredContent"]

        adult = await checklist("adult")
        senior = await checklist("senior")
        assert senior["total_active_minutes"] < adult["total_active_minutes"]
        assert adult["meals_per_day"] == 2
        assert "not veterinary advice" in adult["disclaimer"]

    async def test_water_estimate_is_proportional_and_bounded(self) -> None:
        async def water(weight: float) -> dict:
            reply = await server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "estimate_daily_water_ml",
                        "arguments": {"species": "dog", "weight_kg": weight},
                    },
                }
            )
            return reply["result"]["structuredContent"]

        small, large = await water(5), await water(30)
        assert small["estimated_ml_per_day"] < large["estimated_ml_per_day"]
        assert large["range_low_ml_per_day"] < large["estimated_ml_per_day"] < large["range_high_ml_per_day"]

    @pytest.mark.parametrize("weight", [0, -3, 500])
    async def test_invalid_weight_is_rejected(self, weight: float) -> None:
        reply = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "estimate_daily_water_ml",
                    "arguments": {"species": "dog", "weight_kg": weight},
                },
            }
        )
        assert reply["result"]["isError"] is True

    async def test_unknown_species_is_rejected(self) -> None:
        reply = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "estimate_daily_water_ml",
                    "arguments": {"species": "iguana", "weight_kg": 2},
                },
            }
        )
        assert reply["result"]["isError"] is True


# ------------------------------------------------------- 2. real Streamable HTTP


class TestStreamableHttp:
    async def test_health_endpoint(self, http_endpoint: str) -> None:
        async with httpx2.AsyncClient() as http:
            response = await http.get(f"{http_endpoint}/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_handshake_issues_a_session_id(self, http_endpoint: str) -> None:
        async with httpx2.AsyncClient() as http:
            response = await http.post(f"{http_endpoint}/mcp", json=INITIALIZE)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert SESSION_HEADER.lower() in {k.lower() for k in response.headers}
        assert response.json()["result"]["serverInfo"]["name"] == "pet-care"

    async def test_notification_gets_202_and_no_body(self, http_endpoint: str) -> None:
        """A JSON-RPC notification has no id, so there is nothing to return."""
        async with httpx2.AsyncClient() as http:
            response = await http.post(
                f"{http_endpoint}/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        assert response.status_code == 202
        assert response.content == b""

    async def test_full_session_over_one_connection(self, http_endpoint: str) -> None:
        """Initialize, initialized, tools/list, tools/call, then terminate."""
        async with httpx2.AsyncClient() as http:
            init = await http.post(f"{http_endpoint}/mcp", json=INITIALIZE)
            session_id = init.headers[SESSION_HEADER]
            headers = {SESSION_HEADER: session_id}

            await http.post(
                f"{http_endpoint}/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )
            listed = await http.post(
                f"{http_endpoint}/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers=headers,
            )
            called = await http.post(
                f"{http_endpoint}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "get_daily_care_checklist",
                        "arguments": {
                            "species": "cat",
                            "life_stage": "adult",
                            "energy_level": "medium",
                        },
                    },
                },
                headers=headers,
            )
            ended = await http.request("DELETE", f"{http_endpoint}/mcp", headers=headers)

        assert {t["name"] for t in listed.json()["result"]["tools"]} == TOOL_NAMES
        payload = called.json()["result"]
        assert payload["isError"] is False
        assert payload["structuredContent"]["species"] == "cat"
        assert ended.status_code == 204

    async def test_malformed_body_returns_a_parse_error_not_a_500(self, http_endpoint: str) -> None:
        async with httpx2.AsyncClient() as http:
            response = await http.post(
                f"{http_endpoint}/mcp", content=b"{not json", headers={"content-type": "application/json"}
            )
        assert response.status_code == 200
        assert json.loads(response.text)["error"]["code"] == -32700

    async def test_get_is_declined_rather_than_left_open(self, http_endpoint: str) -> None:
        """This server never initiates a message, so it does not hold a stream open."""
        async with httpx2.AsyncClient() as http:
            response = await http.get(f"{http_endpoint}/mcp")
        assert response.status_code == 405


# --------------------------------------------------- 3. interop with the official SDK


class TestReferenceInterop:
    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_official_sdk_client_can_drive_this_server(self, http_endpoint: str, mode: str) -> None:
        from mcp.client import Client

        async with Client(f"{http_endpoint}/mcp", mode=mode) as client:  # type: ignore[arg-type]
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == TOOL_NAMES
            result = await client.call_tool(
                "get_daily_care_checklist",
                {"species": "cat", "life_stage": "adult", "energy_level": "medium"},
            )

        assert result.is_error is False
        assert result.structured_content["species"] == "cat"
        assert result.structured_content["items"]
