"""Terminal rendering.

All console output goes through this module so the conversation loop in
:mod:`adoptamatch_chatbot.app` stays free of formatting concerns and remains
testable without capturing ANSI output.

``rich`` is used when a real terminal is attached; it degrades gracefully when
output is redirected to a file or a pipe.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from adoptamatch_chatbot.mcp_host.models import ServerStatus, ToolCallOutcome, ToolRef

_STATE_STYLE = {
    "connected": "bold green",
    "failed": "bold red",
    "disabled": "dim",
    "not connected": "yellow",
}


class Presenter:
    """Everything the user sees."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    # ------------------------------------------------------------------ basics

    def rule(self, title: str = "") -> None:
        self.console.rule(title)

    def info(self, message: str) -> None:
        self.console.print(f"[dim]{message}[/dim]")

    def warn(self, message: str) -> None:
        self.console.print(f"[yellow]! {message}[/yellow]")

    def error(self, message: str) -> None:
        self.console.print(f"[bold red]x {message}[/bold red]")

    def success(self, message: str) -> None:
        self.console.print(f"[green]+ {message}[/green]")

    # ------------------------------------------------------------------ banner

    def banner(self, model: str, provider: str, session_id: str, log_path: str) -> None:
        body = Text()
        body.append("AdoptaMatch console host\n", style="bold cyan")
        body.append("An MCP host: one LLM, several MCP servers, one audit log.\n\n")
        body.append(f"provider  {provider}\n")
        body.append(f"model     {model}\n")
        body.append(f"session   {session_id}\n")
        body.append(f"log       {log_path}\n\n")
        body.append("Type /help for commands, /exit to quit.", style="dim")
        self.console.print(Panel(body, border_style="cyan", title="ready"))

    # ---------------------------------------------------------------- commands

    def help(self, commands: dict[str, str]) -> None:
        table = Table(title="Console commands", show_lines=False, header_style="bold")
        table.add_column("command", style="cyan", no_wrap=True)
        table.add_column("what it does")
        for name, description in commands.items():
            table.add_row(name, description)
        self.console.print(table)

    def servers(self, statuses: list[ServerStatus]) -> None:
        table = Table(title="Configured MCP servers", header_style="bold")
        table.add_column("name", style="cyan", no_wrap=True)
        table.add_column("transport", no_wrap=True)
        table.add_column("state", no_wrap=True)
        table.add_column("negotiation", no_wrap=True)
        table.add_column("tools", justify="right", no_wrap=True)
        table.add_column("target / error")
        for status in statuses:
            detail = status.error or status.config.target or status.config.description
            negotiation = status.negotiated_mode or status.config.mode
            if status.protocol_version:
                negotiation = f"{negotiation} / {status.protocol_version}"
            table.add_row(
                status.config.name,
                status.config.transport,
                Text(status.state, style=_STATE_STYLE.get(status.state, "")),
                negotiation if status.config.enabled else "-",
                str(status.tool_count) if status.connected else "-",
                detail,
            )
        self.console.print(table)

    def tools(self, tools: list[ToolRef]) -> None:
        if not tools:
            self.warn("No tools discovered. Check /servers.")
            return
        table = Table(title=f"Discovered tools ({len(tools)})", header_style="bold")
        table.add_column("tool the model sees", style="cyan", no_wrap=True)
        table.add_column("server", no_wrap=True)
        table.add_column("original name", no_wrap=True)
        table.add_column("description")
        for tool in tools:
            table.add_row(
                tool.exposed_name,
                tool.server,
                tool.original_name if tool.is_qualified else "",
                (tool.description or "").split("\n")[0][:90],
            )
        self.console.print(table)

    def logs(self, summary: dict[str, Any], recent: list[dict[str, Any]]) -> None:
        body = Text()
        body.append(f"session id     {summary['session_id']}\n")
        body.append(f"host log       {summary['path']}\n")
        if summary.get("protocol_path"):
            body.append(f"protocol log   {summary['protocol_path']}\n")
        body.append(f"events         {summary['events']}\n")
        body.append(f"tool calls     {summary['tool_calls']}\n")
        body.append(f"errors         {summary['errors']}\n")
        self.console.print(Panel(body, title="interaction log", border_style="blue"))

        if not recent:
            return
        table = Table(title="last events", header_style="bold")
        table.add_column("time", no_wrap=True)
        table.add_column("server", no_wrap=True)
        table.add_column("direction", no_wrap=True)
        table.add_column("method", no_wrap=True)
        table.add_column("tool", no_wrap=True)
        table.add_column("ms", justify="right", no_wrap=True)
        table.add_column("req id", no_wrap=True)
        for record in recent:
            style = "red" if record.get("status") == "error" else ""
            table.add_row(
                str(record.get("timestamp", ""))[11:23],
                record.get("server", ""),
                Text(record.get("direction", ""), style=style),
                record.get("method", ""),
                record.get("tool", "") or "",
                str(record.get("elapsed_ms", "")),
                record.get("request_id", ""),
                style=style,
            )
        self.console.print(table)

    # ------------------------------------------------------------ conversation

    def thinking(self, message: str = "asking the model") -> Any:
        """Context manager showing a spinner while the model or a tool is busy."""
        return self.console.status(f"[dim]{message}...[/dim]", spinner="dots")

    def tool_call(self, name: str, server: str, arguments: dict[str, Any]) -> None:
        rendered = json.dumps(arguments, ensure_ascii=False, default=str)
        if len(rendered) > 300:
            rendered = rendered[:300] + " ..."
        self.console.print(f"[magenta]-> {server}.{name}[/magenta] [dim]{rendered}[/dim]")

    def tool_result(self, outcome: ToolCallOutcome) -> None:
        marker = "[green]<-[/green]" if outcome.ok else "[red]<-[/red]"
        preview = outcome.text.replace("\n", " ")
        if len(preview) > 160:
            preview = preview[:160] + " ..."
        status = "ok" if outcome.ok else "error"
        self.console.print(
            f"{marker} [dim]{outcome.server}.{outcome.tool} {status} "
            f"in {outcome.elapsed_ms} ms (req {outcome.request_id})[/dim]"
        )
        self.console.print(f"   [dim]{preview}[/dim]")

    def assistant(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print()
        self.console.print(Markdown(text))
        self.console.print()
