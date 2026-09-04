"""Run the Filesystem + Git scenario deterministically, without a language model.

Why this exists
---------------
The scenario in ``config/demo-prompts.md`` is meant to be driven by the model in
natural language. This script performs the *same* MCP calls through the *same*
:class:`~adoptamatch_chatbot.mcp_host.manager.MCPManager`, with the model replaced
by a fixed plan. Two uses:

* a contingency for the live demo -- if the API is unreachable, the MCP half of
  the project is still demonstrable, with the same JSONL log as evidence;
* a smoke test that the two reference servers are correctly wired and scoped.

Usage::

    uv run python scripts/demo_filesystem_git.py [--servers config/servers.toml]

It creates ``demo_workspace/demo-repo/notes/`` and a ``README.md`` inside it
via the Filesystem server, then stages and commits that file via the Git server,
and finally prints the resulting log lines.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from adoptamatch_chatbot.config import ConfigError, load_config
from adoptamatch_chatbot.mcp_host.logger import InteractionLogger
from adoptamatch_chatbot.mcp_host.manager import MCPManager
from adoptamatch_chatbot.presentation import Presenter

#: Path of the demo repository as the Git server sees it (relative to the cwd it
#: was launched with, which servers.toml sets to the chatbot repository root).
REPO = "demo_workspace/demo-repo"

README_BODY = f"""# Shelter notes

Created by the AdoptaMatch chatbot through the official Filesystem MCP server
on {date.today().isoformat()}, then committed through the official Git MCP server.

This file exists to demonstrate that the host routes each step to the right
server and records every request and response in the session log.
"""

#: Both servers are scoped, and their scopes are expressed differently:
#: the Filesystem server takes paths relative to the directory it was launched
#: with (``demo_workspace``), while the Git server takes the path of the
#: repository it was launched with (``demo_workspace/demo-repo``), relative to
#: *its* working directory. The file is written inside that repository so the
#: Git server can commit it.
#: (step label, tool name, arguments)
PLAN: list[tuple[str, str, dict]] = [
    (
        "1. Filesystem: confirm the sandbox the server is scoped to",
        "list_allowed_directories",
        {},
    ),
    (
        "2. Filesystem: create the demo directory",
        "create_directory",
        {"path": "demo-repo/notes"},
    ),
    (
        "3. Filesystem: write README.md inside it",
        "write_file",
        {"path": "demo-repo/notes/README.md", "content": README_BODY},
    ),
    (
        "4. Filesystem: read it back",
        "read_text_file",
        {"path": "demo-repo/notes/README.md"},
    ),
    (
        "5. Git: inspect the repository before the change",
        "git_status",
        {"repo_path": REPO},
    ),
    (
        "6. Git: stage the new file",
        "git_add",
        {"repo_path": REPO, "files": ["notes/README.md"]},
    ),
    (
        "7. Git: commit it",
        "git_commit",
        {"repo_path": REPO, "message": "docs: add shelter notes via MCP"},
    ),
    (
        "8. Git: show the resulting history",
        "git_log",
        {"repo_path": REPO, "max_count": 3},
    ),
]

REQUIRED_SERVERS = ("filesystem", "git")


async def run(servers_config: Path | None) -> int:
    presenter = Presenter()
    try:
        config = load_config(servers_config=servers_config, require_api_key=False)
    except ConfigError as exc:
        presenter.error(str(exc))
        return 2

    interaction_log = InteractionLogger(config.log_dir)
    manager = MCPManager(config.servers, interaction_log, config.config_dir)

    try:
        with presenter.thinking("starting MCP servers"):
            await manager.connect_all()
        presenter.servers(manager.statuses)

        missing = [name for name in REQUIRED_SERVERS if name not in manager.connected_servers]
        if missing:
            presenter.error(
                f"This scenario needs the {' and '.join(REQUIRED_SERVERS)} servers, "
                f"but {', '.join(missing)} did not connect. See /servers output above."
            )
            return 1

        failures = 0
        for label, tool, arguments in PLAN:
            presenter.rule(label)
            presenter.tool_call(tool, manager_owner(manager, tool), arguments)
            outcome = await manager.call_tool(tool, arguments)
            presenter.tool_result(outcome)
            if not outcome.ok:
                failures += 1

        presenter.rule("session log")
        presenter.logs(interaction_log.summary(), interaction_log.tail(len(PLAN) * 2))
        if failures:
            presenter.warn(f"{failures} step(s) reported an error. See the log above.")
        else:
            presenter.success("Every step succeeded. The log above is the evidence.")
        return 1 if failures else 0
    finally:
        with presenter.thinking("closing MCP servers"):
            await manager.aclose()


def manager_owner(manager: MCPManager, tool: str) -> str:
    for reference in manager.tools:
        if reference.exposed_name == tool:
            return reference.server
    return "?"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", type=Path, default=None, help="Path to servers.toml.")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.servers)))


if __name__ == "__main__":
    main()
