"""The hand-written MCP client: framing, both transports, and SDK interoperability.

The project claims to implement MCP by exchanging JSON-RPC directly, with no MCP
SDK. Two things have to be true for that claim to hold, and both are asserted
here:

* nothing in the shipped package imports an MCP SDK (``TestNoSdkAtRuntime``);
* the hand-written client still talks correctly to a *reference* implementation
  (``TestSdkInterop``), over both transports.

The official SDK is a development dependency, used the way a conformance suite is
used. The fixture servers in ``tests/servers/`` are built with it on purpose, so
every test in ``test_manager.py`` is also an interoperability test.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from adoptforme_chatbot.mcp_wire import (
    ClientSession,
    ProtocolError,
    StdioTransport,
    StreamableHttpTransport,
    TransportError,
)
from adoptforme_chatbot.mcp_wire.messages import (
    ERROR_CODES,
    IdAllocator,
    classify,
    is_lifecycle,
    notification,
    raise_for_error,
    request,
)
from adoptforme_chatbot.mcp_wire.transports import resolve_executable
from tests.conftest import SERVERS_DIR

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "adoptforme_chatbot"


# --------------------------------------------------------------------- framing


class TestFraming:
    def test_a_request_has_an_id_and_a_notification_does_not(self) -> None:
        req = request("tools/list", None, 1)
        note = notification("notifications/initialized")
        assert req == {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert "id" not in note
        assert classify(req) == "request"
        assert classify(note) == "notification"

    def test_params_are_omitted_when_absent(self) -> None:
        assert "params" not in request("ping", None, 3)
        assert request("tools/call", {"name": "x"}, 4)["params"] == {"name": "x"}

    def test_classify_covers_all_four_kinds(self) -> None:
        assert classify({"jsonrpc": "2.0", "id": 1, "result": {}}) == "response"
        assert classify({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}) == "error"
        assert classify({"jsonrpc": "2.0"}) == "unknown"

    def test_lifecycle_messages_are_recognised(self) -> None:
        assert is_lifecycle(request("initialize", {}, 1)) is True
        assert is_lifecycle(notification("notifications/initialized")) is True
        assert is_lifecycle(request("tools/call", {}, 2)) is False

    def test_ids_are_monotonic_so_a_capture_reads_in_order(self) -> None:
        allocator = IdAllocator()
        assert [allocator.next() for _ in range(3)] == [1, 2, 3]

    def test_raise_for_error_returns_the_result(self) -> None:
        assert raise_for_error({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) == {"ok": True}

    @pytest.mark.parametrize("code", sorted(ERROR_CODES))
    def test_every_reserved_error_code_is_named(self, code: int) -> None:
        with pytest.raises(ProtocolError) as caught:
            raise_for_error({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "nope"}})
        assert caught.value.code == code
        assert ERROR_CODES[code] in str(caught.value)

    def test_a_reply_with_neither_result_nor_error_is_rejected(self) -> None:
        with pytest.raises(ProtocolError, match="not conforming"):
            raise_for_error({"jsonrpc": "2.0", "id": 1})

    def test_executable_resolution_finds_the_interpreter(self) -> None:
        assert Path(resolve_executable(sys.executable)).exists()


# ------------------------------------------------------------- stdio transport


class TestStdioTransport:
    async def test_handshake_records_the_server_identity(self) -> None:
        session = ClientSession(
            StdioTransport(sys.executable, [str(SERVERS_DIR / "alpha_server.py")]), timeout=20
        )
        try:
            identity = await session.initialize()
            assert identity.name == "alpha"
            assert identity.protocol_version
            assert session.initialized is True
        finally:
            await session.aclose()

    async def test_every_frame_is_offered_to_the_hook_in_both_directions(self) -> None:
        seen: list[tuple[str, str | None, str]] = []

        def hook(direction: str, message: dict) -> None:
            seen.append((direction, message.get("method"), classify(message)))

        transport = StdioTransport(sys.executable, [str(SERVERS_DIR / "alpha_server.py")], on_frame=hook)
        session = ClientSession(transport, timeout=20)
        try:
            await session.initialize()
            await session.list_tools()
            await session.call_tool("add", {"a": 1, "b": 2})
        finally:
            await session.aclose()

        assert ("out", "initialize", "request") in seen
        assert ("out", "notifications/initialized", "notification") in seen
        assert ("out", "tools/list", "request") in seen
        assert ("out", "tools/call", "request") in seen
        assert sum(1 for direction, _, _ in seen if direction == "in") == 3

    async def test_a_slow_tool_hits_the_timeout_without_hanging(self) -> None:
        session = ClientSession(
            StdioTransport(sys.executable, [str(SERVERS_DIR / "slow_server.py")]), timeout=20
        )
        try:
            await session.initialize()
            with pytest.raises(TransportError, match="No reply"):
                await session.call_tool("nap", {"seconds": 30}, timeout=1.0)
        finally:
            await session.aclose()

    async def test_a_server_that_never_starts_is_reported(self) -> None:
        session = ClientSession(StdioTransport("definitely-not-a-real-command-xyz"), timeout=5)
        with pytest.raises(TransportError, match="Could not start"):
            await session.initialize()

    async def test_a_server_that_exits_immediately_is_reported(self) -> None:
        session = ClientSession(StdioTransport(sys.executable, ["-c", "raise SystemExit(1)"]), timeout=10)
        with pytest.raises(TransportError):
            await session.initialize()
        await session.aclose()

    async def test_noise_on_stdout_does_not_break_the_framing(self, tmp_path: Path) -> None:
        """A server that prints to stdout is a protocol violation, not a fatal one."""
        script = tmp_path / "noisy.py"
        script.write_text(
            "import json, sys\n"
            "print('this line is not JSON-RPC')\n"
            "sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if 'id' not in message:\n"
            "        continue\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'],\n"
            "                      'result': {'protocolVersion': '2025-06-18',\n"
            "                                 'capabilities': {},\n"
            "                                 'serverInfo': {'name': 'noisy', 'version': '1'}}}))\n"
            "    sys.stdout.flush()\n",
            encoding="utf-8",
        )
        errlog = (tmp_path / "stderr.log").open("w", encoding="utf-8")
        session = ClientSession(StdioTransport(sys.executable, [str(script)], errlog=errlog), timeout=15)
        try:
            identity = await session.initialize()
            assert identity.name == "noisy"
        finally:
            await session.aclose()
            errlog.close()
        assert "discarded non-JSON stdout line" in (tmp_path / "stderr.log").read_text(encoding="utf-8")

    async def test_closing_twice_is_safe(self) -> None:
        session = ClientSession(
            StdioTransport(sys.executable, [str(SERVERS_DIR / "alpha_server.py")]), timeout=20
        )
        await session.initialize()
        await session.aclose()
        await session.aclose()


# ---------------------------------------------------- Streamable HTTP transport


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def sdk_http_server() -> Iterator[str]:
    """A Streamable HTTP server built with the **official SDK**, on a real socket.

    Pointing the hand-written HTTP client at a reference server is the strongest
    available check that the transport is implemented correctly.
    """
    import uvicorn
    from mcp.server.mcpserver import MCPServer

    reference = MCPServer(name="reference", version="1.0.0", instructions="Reference server.")

    @reference.tool(description="Echo the given text back.")
    def echo(text: str) -> str:
        return f"reference echoes {text}"

    @reference.tool(description="Multiply two integers.")
    def multiply(a: int, b: int) -> int:
        return a * b

    port = _free_port()
    app = reference.streamable_http_app(streamable_http_path="/mcp")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while not running.started:
        if time.time() > deadline:  # pragma: no cover - CI safety net
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        running.should_exit = True
        thread.join(timeout=10)


class TestStreamableHttpTransport:
    async def test_handshake_and_tool_call_over_tcp(self, sdk_http_server: str) -> None:
        session = ClientSession(StreamableHttpTransport(sdk_http_server), timeout=30)
        try:
            identity = await session.initialize()
            assert identity.name == "reference"
            tools = await session.list_tools()
            assert {tool.name for tool in tools} == {"echo", "multiply"}
            result = await session.call_tool("echo", {"text": "hello"})
        finally:
            await session.aclose()
        assert result.is_error is False
        assert "reference echoes hello" in result.text

    async def test_the_session_id_is_captured_and_echoed(self, sdk_http_server: str) -> None:
        transport = StreamableHttpTransport(sdk_http_server)
        session = ClientSession(transport, timeout=30)
        try:
            await session.initialize()
            assert transport.session_id, "the server issued no Mcp-Session-Id"
            await session.call_tool("multiply", {"a": 6, "b": 7})
        finally:
            await session.aclose()

    async def test_structured_output_is_preferred_over_prose(self, sdk_http_server: str) -> None:
        session = ClientSession(StreamableHttpTransport(sdk_http_server), timeout=30)
        try:
            await session.initialize()
            result = await session.call_tool("multiply", {"a": 6, "b": 7})
        finally:
            await session.aclose()
        assert json.loads(result.text) == {"result": 42}
        assert result.structured == {"result": 42}

    async def test_an_unreachable_endpoint_is_reported_not_hung(self) -> None:
        session = ClientSession(
            StreamableHttpTransport(f"http://127.0.0.1:{_free_port()}/mcp", connect_timeout=3),
            timeout=3,
        )
        with pytest.raises(TransportError):
            await session.initialize()
        await session.aclose()


# ------------------------------------------------------ interop, the other way


class TestSdkInterop:
    async def test_the_official_sdk_client_can_drive_our_fixture_servers(self) -> None:
        """Sanity check on the oracle itself, so a failure above is unambiguous."""
        from mcp.client import Client
        from mcp.client.stdio import StdioServerParameters

        params = StdioServerParameters(command=sys.executable, args=[str(SERVERS_DIR / "alpha_server.py")])
        async with Client(params, mode="legacy") as client:
            listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {"ping", "add", "boom"}


# --------------------------------------------------------- no SDK at runtime


class TestNoSdkAtRuntime:
    def test_no_module_in_the_package_imports_an_mcp_sdk(self) -> None:
        offenders: list[str] = []
        for path in PACKAGE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for number, line in enumerate(source.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith(("import mcp", "from mcp ", "from mcp.")):
                    offenders.append(f"{path.name}:{number}: {stripped}")
        assert offenders == [], "the package must not import an MCP SDK: " + "; ".join(offenders)

    def test_importing_the_package_does_not_load_an_mcp_sdk(self) -> None:
        """Belt and braces: check the real import graph, not just the source text."""
        code = (
            "import sys, adoptforme_chatbot.cli, adoptforme_chatbot.mcp_host.manager;"
            "print([m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')])"
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert completed.stdout.strip() == "[]", completed.stdout

    def test_the_declared_runtime_dependencies_contain_no_mcp_sdk(self) -> None:
        pyproject = (PACKAGE_ROOT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        runtime = pyproject.split("[dependency-groups]")[0]
        assert '"mcp' not in runtime
