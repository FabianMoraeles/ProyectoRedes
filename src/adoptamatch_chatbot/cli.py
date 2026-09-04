"""Command-line entry point: ``adoptamatch-chatbot``.

Start-up order matters and is deliberate:

1. parse the arguments;
2. load and **validate** the configuration -- a bad ``.env`` or ``servers.toml``
   must fail here, with an actionable message and no traceback;
3. open the session log, so even a failed connection is on record;
4. connect the MCP servers, tolerating individual failures;
5. build the LLM provider;
6. run the chat loop;
7. close every client, stream and subprocess, whatever happened.

Step 7 lives in a ``finally`` block: Ctrl-C during a tool call still shuts the
subprocesses down.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from adoptamatch_chatbot import __version__
from adoptamatch_chatbot.app import ChatApp
from adoptamatch_chatbot.config import AppConfig, ConfigError, load_config
from adoptamatch_chatbot.llm.base import LLMError, LLMProvider
from adoptamatch_chatbot.llm.scripted import ScriptedProvider, offline_responder
from adoptamatch_chatbot.mcp_host.logger import InteractionLogger
from adoptamatch_chatbot.mcp_host.manager import MCPManager
from adoptamatch_chatbot.presentation import Presenter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adoptamatch-chatbot",
        description="Console chatbot that acts as an MCP host over several MCP servers.",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Path to the .env file.")
    parser.add_argument(
        "--servers",
        type=Path,
        default=None,
        help="Path to servers.toml. Defaults to $MCP_SERVERS_CONFIG or config/servers.toml.",
    )
    parser.add_argument("--model", default=None, help="Override ANTHROPIC_MODEL for this run.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run without an API key using a scripted stand-in. Exercises the MCP plumbing only.",
    )
    parser.add_argument(
        "--no-protocol-log",
        action="store_true",
        help="Do not write the raw JSON-RPC message log.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colour. The NO_COLOR environment variable does the same.",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Use ASCII glyphs instead of Unicode, for terminals that cannot render them.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Start with full tool arguments and results shown. Toggle later with /verbose.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the configuration, connect, list the tools and exit.",
    )
    parser.add_argument("--version", action="version", version=f"adoptamatch-chatbot {__version__}")
    return parser


def build_provider(config: AppConfig, offline: bool) -> LLMProvider:
    """Pick the provider. The rest of the app never learns which one it got."""
    if offline:
        return ScriptedProvider(responder=offline_responder, model="offline-router")
    from adoptamatch_chatbot.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key=config.anthropic_api_key, model=config.model)


async def run(args: argparse.Namespace, config: AppConfig, presenter: Presenter) -> int:
    interaction_log = InteractionLogger(config.log_dir, protocol_log=not args.no_protocol_log)
    manager = MCPManager(config.servers, interaction_log, config.config_dir)

    try:
        provider = build_provider(config, args.offline)
    except LLMError as exc:
        presenter.error(str(exc))
        return 2

    presenter.banner(
        model=provider.model,
        provider=provider.name,
        session_id=interaction_log.session_id,
        log_path=str(interaction_log.path),
    )

    try:
        with presenter.thinking("starting MCP servers"):
            await manager.connect_all()

        presenter.servers(manager.statuses)
        failed = [status for status in manager.statuses if status.state == "failed"]
        if failed:
            presenter.warn(
                f"{len(failed)} server(s) failed to start; the rest of the session works normally."
            )
        if not manager.connected_servers:
            presenter.warn("No MCP server is connected. The assistant can still answer general questions.")
        else:
            presenter.success(
                f"{len(manager.tools)} tool(s) discovered across "
                f"{len(manager.connected_servers)} server(s). Type /tools to list them."
            )

        if args.check:
            presenter.tools(manager.tools)
            presenter.logs(interaction_log.summary(), interaction_log.tail(20))
            return 0 if manager.connected_servers else 1

        app = ChatApp(
            provider=provider,
            manager=manager,
            interaction_log=interaction_log,
            presenter=presenter,
            max_tool_iterations=config.max_tool_iterations,
        )
        await app.run()
        return 0
    finally:
        with presenter.thinking("closing MCP servers"):
            await manager.aclose()
        summary = interaction_log.summary()
        presenter.info(
            f"Session {summary['session_id']} closed: {summary['events']} logged events, "
            f"{summary['tool_calls']} tool call(s). Log: {summary['path']}"
        )


def main() -> None:
    args = build_parser().parse_args()
    presenter = Presenter(no_color=args.no_color, ascii_only=args.ascii, verbose=args.verbose)

    try:
        config = load_config(
            env_file=args.env_file,
            servers_config=args.servers,
            require_api_key=not args.offline,
        )
    except ConfigError as exc:
        presenter.error(str(exc))
        raise SystemExit(2) from None

    if args.model:
        config.model = args.model

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # The HTTP client logs one INFO line per request, which drowns the console
    # during a Streamable HTTP session. The same exchange is already in the JSONL
    # log, so raise its threshold unless the user explicitly asked for DEBUG.
    if config.log_level != "DEBUG":
        for noisy in ("httpx2", "httpcore2", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        exit_code = asyncio.run(run(args, config, presenter))
    except KeyboardInterrupt:
        presenter.info("Interrupted.")
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
