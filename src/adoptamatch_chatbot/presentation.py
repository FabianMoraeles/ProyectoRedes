"""Terminal user interface.

All console output goes through this module, so the conversation loop in
:mod:`adoptamatch_chatbot.app` stays free of formatting and remains testable
without capturing ANSI codes.

The design rationale -- colour choices, hierarchy, feedback, accessibility -- is
written up in ``docs/ui-design.md``. The short version, because it explains every
decision below:

**One glyph vocabulary.** Each kind of line is introduced by a fixed symbol in a
fixed column, so a transcript can be skimmed without being read:

===========  ========================================================
``>``        you: the prompt where you type
``|>``       a tool call the host is about to make
``+`` ``x``  a tool result: succeeded / failed
``!``        a warning
``-``        neutral information
===========  ========================================================

(The table shows the ASCII fallback; a capable terminal gets the Unicode set.)

**Colour is never the only signal.** Every state carries a glyph *and* a word, so
the interface still works for a colour-blind reader, in a monochrome terminal,
piped to a file, or with ``--no-color``.

**Hierarchy by weight, not by noise.** The assistant's answer is the thing you
actually read, so it gets the plain foreground colour and the most space. Tool
activity is machinery: indented and dimmed so it recedes. Errors are the only
thing allowed to be loud.

**Progressive disclosure.** Compact output by default; ``/verbose`` reveals full
arguments, full results and per-call timings for the same session.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from rich.box import ROUNDED, SIMPLE
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from adoptamatch_chatbot.mcp_host.models import ServerStatus, ToolCallOutcome, ToolRef

# --------------------------------------------------------------------- design tokens

#: Semantic palette. Hues stay distinguishable under the common forms of colour
#: blindness: the two states most often confused, success and failure, are
#: separated by glyph and wording first and only then by green/red. Cyan carries
#: identity, magenta marks machine activity -- neither collides with that pair.
STYLE = {
    "brand": "bold cyan",
    "user": "bold bright_white",
    "assistant": "default",
    "tool": "magenta",
    "ok": "green",
    "error": "bold red",
    "warn": "yellow",
    "meta": "dim",
    "heading": "bold",
}

#: Unicode glyphs, with an ASCII fallback for terminals that cannot encode them.
GLYPHS_UNICODE = {
    "prompt": "›",  # ›
    "call": "▸",  # ▸
    "ok": "✓",  # ✓
    "error": "✗",  # ✗
    "warn": "!",
    "info": "·",  # ·
    "bullet": "•",  # •
}
GLYPHS_ASCII = {
    "prompt": ">",
    "call": "|>",
    "ok": "+",
    "error": "x",
    "warn": "!",
    "info": "-",
    "bullet": "*",
}

_STATE_STYLE = {
    "connected": "green",
    "failed": "bold red",
    "disabled": "dim",
    "not connected": "yellow",
}

#: Compact mode caps how much of an argument list or a result is shown inline.
COMPACT_ARGUMENT_CHARS = 110
COMPACT_RESULT_CHARS = 160


def _supports_unicode(console: Console) -> bool:
    """Guess whether the terminal can render the glyph set without mojibake."""
    encoding = getattr(console.file, "encoding", None) or sys.getdefaultencoding()
    try:
        "".join(GLYPHS_UNICODE.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Presenter:
    """Everything the user sees.

    Args:
        console: An explicit console, mostly for tests.
        no_color: Disable colour. ``NO_COLOR`` in the environment does the same,
            following the convention at https://no-color.org/.
        ascii_only: Force the ASCII glyph set.
        verbose: Start in verbose mode.
    """

    def __init__(
        self,
        console: Console | None = None,
        *,
        no_color: bool = False,
        ascii_only: bool = False,
        verbose: bool = False,
    ) -> None:
        disable_colour = no_color or bool(os.environ.get("NO_COLOR"))
        self.console = console or Console(no_color=disable_colour)
        self.verbose = verbose
        self.glyphs = GLYPHS_ASCII if (ascii_only or not _supports_unicode(self.console)) else GLYPHS_UNICODE

    # ------------------------------------------------------------------ basics

    def set_verbose(self, verbose: bool) -> None:
        self.verbose = verbose

    def _line(self, glyph_key: str, message: str, style: str, label: str = "") -> None:
        """One status line: glyph, optional word, then the message.

        The glyph and the word together carry the meaning; the colour only
        reinforces it.
        """
        text = Text()
        text.append(f"{self.glyphs[glyph_key]} ", style=style)
        if label:
            text.append(f"{label} ", style=style)
        text.append(message)
        self.console.print(text)

    def rule(self, title: str = "") -> None:
        self.console.rule(Text(title, style=STYLE["meta"]), style="dim")

    def info(self, message: str) -> None:
        self.console.print(Text(f"{self.glyphs['info']} {message}", style=STYLE["meta"]))

    def warn(self, message: str) -> None:
        self._line("warn", message, STYLE["warn"], "warning")

    def error(self, message: str) -> None:
        self._line("error", message, STYLE["error"], "error")

    def success(self, message: str) -> None:
        self._line("ok", message, STYLE["ok"])

    def prompt_text(self) -> str:
        """The string ``input()`` shows. Short, distinct, always in the same place."""
        return f"you {self.glyphs['prompt']} "

    # ------------------------------------------------------------------ banner

    def banner(self, model: str, provider: str, session_id: str, log_path: str) -> None:
        """The first screen: what this is, what it is connected to, what to type."""
        title = Text("AdoptaMatch", style=STYLE["brand"])
        title.append("  console MCP host", style=STYLE["meta"])

        facts = Table.grid(padding=(0, 2))
        facts.add_column(style=STYLE["meta"], justify="right", no_wrap=True)
        facts.add_column()
        facts.add_row("model", model)
        facts.add_row("provider", provider)
        facts.add_row("session", session_id)
        facts.add_row("log", log_path)

        hint = Text()
        hint.append("Ask anything, or describe your home to get a pet recommendation.\n")
        bullet = self.glyphs["bullet"]
        for command, caption in (
            ("/help", "commands"),
            ("/servers", "what is connected"),
            ("/exit", "quit"),
        ):
            hint.append(f"{bullet} {command} ", style=STYLE["brand"])
            hint.append(f"{caption}   ", style=STYLE["meta"])

        self.console.print()
        self.console.print(
            Panel(
                Group(title, Text(), facts, Text(), hint),
                box=ROUNDED,
                border_style="cyan",
                padding=(1, 2),
            )
        )

    # ---------------------------------------------------------------- commands

    def help(self, commands: dict[str, str]) -> None:
        table = Table(box=SIMPLE, show_header=True, header_style=STYLE["heading"], pad_edge=False)
        table.add_column("command", style=STYLE["brand"], no_wrap=True)
        table.add_column("what it does")
        for name, description in commands.items():
            table.add_row(name, description)
        self.console.print()
        self.console.print(table)

    def servers(self, statuses: list[ServerStatus]) -> None:
        """One row per configured server. State is a word first, a colour second."""
        table = Table(
            title="MCP servers",
            title_style=STYLE["heading"],
            title_justify="left",
            box=SIMPLE,
            header_style=STYLE["heading"],
            pad_edge=False,
        )
        table.add_column("name", style=STYLE["brand"], no_wrap=True)
        table.add_column("transport", no_wrap=True)
        table.add_column("state", no_wrap=True)
        table.add_column("protocol", no_wrap=True)
        table.add_column("tools", justify="right", no_wrap=True)
        table.add_column("target / reason")
        for status in statuses:
            detail = status.error or status.config.target or status.config.description
            glyph = {"connected": self.glyphs["ok"], "failed": self.glyphs["error"]}.get(
                status.state, self.glyphs["info"]
            )
            table.add_row(
                status.config.name,
                status.config.transport,
                Text(f"{glyph} {status.state}", style=_STATE_STYLE.get(status.state, "")),
                status.protocol_version or "-",
                str(status.tool_count) if status.connected else "-",
                Text(detail, style="" if status.connected else STYLE["meta"]),
            )
        self.console.print()
        self.console.print(table)

    def tools(self, tools: list[ToolRef]) -> None:
        if not tools:
            self.warn("No tools discovered. Run /servers to see why.")
            return
        table = Table(
            title=f"Tools available to the model ({len(tools)})",
            title_style=STYLE["heading"],
            title_justify="left",
            box=SIMPLE,
            header_style=STYLE["heading"],
            pad_edge=False,
        )
        table.add_column("name the model sees", style=STYLE["brand"], no_wrap=True)
        table.add_column("server", no_wrap=True)
        table.add_column("original name", no_wrap=True, style=STYLE["meta"])
        table.add_column("what it does")
        for tool in tools:
            table.add_row(
                tool.exposed_name,
                tool.server,
                tool.original_name if tool.is_qualified else "",
                (tool.description or "").split("\n")[0][:90],
            )
        self.console.print()
        self.console.print(table)
        qualified = [tool for tool in tools if tool.is_qualified]
        if qualified:
            self.info(
                f"{len(qualified)} tool name(s) collided across servers and were qualified "
                "as <server>__<tool>."
            )

    def logs(self, summary: dict[str, Any], recent: list[dict[str, Any]]) -> None:
        facts = Table.grid(padding=(0, 2))
        facts.add_column(style=STYLE["meta"], justify="right", no_wrap=True)
        facts.add_column()
        facts.add_row("session", str(summary["session_id"]))
        facts.add_row("host log", str(summary["path"]))
        if summary.get("protocol_path"):
            facts.add_row("wire log", str(summary["protocol_path"]))
        facts.add_row("events", str(summary["events"]))
        facts.add_row("tool calls", str(summary["tool_calls"]))
        facts.add_row(
            "errors",
            Text(str(summary["errors"]), style=STYLE["error"] if summary["errors"] else ""),
        )
        frames = summary.get("frames") or {}
        if frames:
            facts.add_row(
                "JSON-RPC frames",
                "  ".join(f"{kind}: {count}" for kind, count in sorted(frames.items())),
            )
        self.console.print()
        self.console.print(
            Panel(facts, title="interaction log", title_align="left", box=ROUNDED, border_style="blue")
        )

        if not recent:
            return
        table = Table(box=SIMPLE, header_style=STYLE["heading"], pad_edge=False)
        table.add_column("time", no_wrap=True, style=STYLE["meta"])
        table.add_column("server", no_wrap=True)
        table.add_column("direction", no_wrap=True)
        table.add_column("method", no_wrap=True)
        table.add_column("tool", no_wrap=True)
        table.add_column("ms", justify="right", no_wrap=True)
        table.add_column("request id", no_wrap=True, style=STYLE["meta"])
        for record in recent:
            failed = record.get("status") == "error"
            table.add_row(
                str(record.get("timestamp", ""))[11:23],
                record.get("server", ""),
                Text(record.get("direction", ""), style=STYLE["error"] if failed else ""),
                record.get("method", ""),
                record.get("tool", "") or "",
                str(record.get("elapsed_ms", "")),
                record.get("request_id", ""),
            )
        self.console.print(table)

    # ------------------------------------------------------------ conversation

    def thinking(self, message: str = "thinking") -> Any:
        """A spinner that says *what* is happening, not just that something is."""
        return self.console.status(Text(f"{message}...", style=STYLE["meta"]), spinner="dots")

    def tool_call(self, name: str, server: str, arguments: dict[str, Any]) -> None:
        """Announce a tool call before it runs, so a slow call is never a mystery."""
        rendered = json.dumps(arguments, ensure_ascii=False, default=str)
        if not self.verbose and len(rendered) > COMPACT_ARGUMENT_CHARS:
            rendered = rendered[:COMPACT_ARGUMENT_CHARS] + " ..."
        line = Text()
        line.append(f"{self.glyphs['call']} ", style=STYLE["tool"])
        line.append(server, style=STYLE["tool"])
        line.append(".", style=STYLE["meta"])
        line.append(name, style=STYLE["tool"])
        self.console.print(Padding(line, (0, 0, 0, 2)))
        self.console.print(Padding(Text(rendered, style=STYLE["meta"]), (0, 0, 0, 4)))

    def tool_result(self, outcome: ToolCallOutcome) -> None:
        """Report the outcome with a glyph, a word, a duration and a correlation id."""
        glyph = self.glyphs["ok"] if outcome.ok else self.glyphs["error"]
        style = STYLE["ok"] if outcome.ok else STYLE["error"]
        word = "ok" if outcome.ok else "failed"

        headline = Text()
        headline.append(f"{glyph} {word}", style=style)
        headline.append(f"  {outcome.elapsed_ms} ms", style=STYLE["meta"])
        headline.append(f"  req {outcome.request_id}", style=STYLE["meta"])
        self.console.print(Padding(headline, (0, 0, 0, 2)))

        preview = outcome.text if self.verbose else outcome.text.replace("\n", " ")
        if not self.verbose and len(preview) > COMPACT_RESULT_CHARS:
            preview = preview[:COMPACT_RESULT_CHARS] + " ..."
        self.console.print(Padding(Text(preview, style=STYLE["meta"]), (0, 0, 1, 4)))

    def assistant(self, text: str) -> None:
        """The answer: the one thing rendered at full weight and full width."""
        if not text.strip():
            return
        self.console.print()
        self.console.print(Padding(Markdown(text), (0, 0, 0, 2)))
        self.console.print()

    def turn_summary(self, tool_calls: int, elapsed_ms: int) -> None:
        """A quiet footer, so cost and latency stay visible but never loud."""
        if tool_calls == 0 and elapsed_ms < 1000:
            return
        parts = [f"{elapsed_ms / 1000:.1f}s"]
        if tool_calls:
            parts.append(f"{tool_calls} tool call{'s' if tool_calls != 1 else ''}")
        separator = f"  {self.glyphs['info']}  "
        self.console.print(Text("  " + separator.join(parts), style=STYLE["meta"]))
