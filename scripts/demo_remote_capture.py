"""Drive the remote MCP server deterministically, for a Wireshark capture.

Why this exists
---------------
Requirement 8 asks for a capture of the traffic between the host and the *remote*
server, with the JSON-RPC messages classified. Driving that from a live model
conversation works, but it is not reproducible: the model decides how many calls
to make and when.

This script performs a fixed sequence against ``pet_care_remote`` only, through
the same :class:`~adoptforme_chatbot.mcp_host.manager.MCPManager` and the same
hand-written client the chatbot uses. Every packet it produces is one of the
messages listed below, in this order, so the capture can be read line by line.

The sequence, and what each step puts on the wire::

    1  initialize                   request  + response   (synchronisation)
    2  notifications/initialized    notification          (synchronisation)
    3  tools/list                   request  + response   (call)
    4  tools/call  daily checklist  request  + response   (call)
    5  tools/call  water estimate   request  + response   (call)
    6  tools/call  invalid weight   request  + response   (call, isError)
    7  DELETE /mcp                  session termination

Usage::

    uv run python scripts/demo_remote_capture.py
    uv run python scripts/demo_remote_capture.py --url http://127.0.0.1:8080/mcp

It prints the wire-log summary at the end: the count of frames by direction, kind
and whether they belong to the lifecycle handshake. That table is what the report
needs, and it should match what Wireshark shows.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
from pathlib import Path

from adoptforme_chatbot.config import ConfigError, load_config
from adoptforme_chatbot.mcp_host.logger import InteractionLogger
from adoptforme_chatbot.mcp_host.manager import MCPManager
from adoptforme_chatbot.mcp_host.models import ServerConfig
from adoptforme_chatbot.presentation import Presenter

DEFAULT_SERVER = "pet_care_remote"

#: (step label, tool name, arguments, expect_ok)
PLAN: list[tuple[str, str, dict, bool]] = [
    (
        "Daily care checklist for an adult, medium-energy dog",
        "get_daily_care_checklist",
        {"species": "dog", "life_stage": "adult", "energy_level": "medium"},
        True,
    ),
    (
        "Water estimate for an 18 kg dog",
        "estimate_daily_water_ml",
        {"species": "dog", "weight_kg": 18},
        True,
    ),
    (
        "Water estimate with an impossible weight (expected to fail)",
        "estimate_daily_water_ml",
        {"species": "dog", "weight_kg": 500},
        False,
    ),
]


def _resolve_server(config, url: str | None, name: str) -> ServerConfig:
    """Find the remote server entry, or build one from an explicit ``--url``."""
    if url:
        return ServerConfig(
            name=name,
            transport="streamable-http",
            enabled=True,
            description="Remote server given on the command line",
            url=url,
            timeout_seconds=30.0,
            connect_timeout_seconds=30.0,
        )
    for entry in config.servers:
        if entry.name == name:
            if entry.transport != "streamable-http":
                raise ConfigError(f"Server '{name}' is not a streamable-http server.")
            # Force it on: the whole point of this script is to exercise it.
            return ServerConfig(**{**entry.__dict__, "enabled": True})
    raise ConfigError(
        f"No server named '{name}' in the configuration. Add it to config/servers.toml, "
        "or pass --url http://127.0.0.1:8080/mcp."
    )


async def run(servers_config: Path | None, url: str | None, name: str) -> int:
    presenter = Presenter()
    try:
        config = load_config(servers_config=servers_config, require_api_key=False)
        server = _resolve_server(config, url, name)
    except ConfigError as exc:
        presenter.error(str(exc))
        return 2

    interaction_log = InteractionLogger(config.log_dir)
    manager = MCPManager([server], interaction_log, config.config_dir)

    presenter.info(f"Wire log: {interaction_log.protocol_path}")
    presenter.info(f"Host log: {interaction_log.path}")

    try:
        presenter.rule("1-3  handshake and discovery")
        with presenter.thinking(f"connecting to {server.url}"):
            await manager.connect_all()
        presenter.servers(manager.statuses)

        if name not in manager.connected_servers:
            presenter.error(
                f"'{name}' did not connect. Start it with:\n"
                "    cd remote_server && HOST=127.0.0.1 PORT=8080 uv run pet-care-mcp"
            )
            return 1
        presenter.tools(manager.tools)

        failures = 0
        for index, (label, tool, arguments, expect_ok) in enumerate(PLAN, start=4):
            presenter.rule(f"{index}  {label}")
            presenter.tool_call(tool, name, arguments)
            outcome = await manager.call_tool(tool, arguments)
            presenter.tool_result(outcome)
            if outcome.ok is not expect_ok:
                failures += 1
                presenter.warn(f"Expected this call to {'succeed' if expect_ok else 'fail'}, and it did not.")
        return 1 if failures else 0
    finally:
        presenter.rule("7  session termination")
        with presenter.thinking("closing the session (DELETE /mcp)"):
            await manager.aclose()
        _summarise(presenter, interaction_log.protocol_path)


def _summarise(presenter: Presenter, wire_log: Path) -> None:
    """Print the frame table the network report needs."""
    if not wire_log.exists():
        presenter.warn("No wire log was written.")
        return
    rows = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = collections.Counter(
        (row["direction"], row["kind"], "synchronisation" if row["lifecycle"] else "call") for row in rows
    )

    presenter.rule("JSON-RPC frames on the wire")
    for (direction, kind, group), count in sorted(counts.items()):
        arrow = "host -> server" if direction == "out" else "server -> host"
        presenter.info(f"{arrow:<15} {kind:<13} {group:<16} {count}")
    presenter.info(f"{'total':<15} {'':<13} {'':<16} {len(rows)}")

    presenter.rule("frame by frame (match this against the capture)")
    for row in rows:
        arrow = "->" if row["direction"] == "out" else "<-"
        marker = "SYNC" if row["lifecycle"] else "call"
        identifier = "" if row["id"] is None else f"id={row['id']}"
        presenter.info(f"  {arrow} {marker:<4} {row['kind']:<13} {row['method'] or '':<26} {identifier}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", type=Path, default=None, help="Path to servers.toml.")
    parser.add_argument(
        "--url",
        default=None,
        help="Override the endpoint, e.g. http://127.0.0.1:8080/mcp or a deployed https:// URL.",
    )
    parser.add_argument("--name", default=DEFAULT_SERVER, help="Server entry to use.")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.servers, args.url, args.name)))


if __name__ == "__main__":
    main()
