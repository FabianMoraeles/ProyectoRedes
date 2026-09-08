"""The chatbot itself: the conversation loop and the console commands.

One turn, end to end:

1. read the user's line;
2. append it to the session history (:mod:`adoptforme_chatbot.conversation`);
3. send system prompt + full history + every discovered tool schema to the LLM;
4. if the model asked for tools, resolve each one to its owning MCP server;
5. execute them through the MCP manager, which logs request, response, error and
   duration with a shared ``request_id``;
6. append the results to the history and ask the model again;
7. repeat until the model stops calling tools (bounded by ``max_tool_iterations``);
8. print the final answer;
9. loop until ``/exit``.

The loop never lets an MCP failure or an LLM error end the session: both are
reported and the user can keep talking.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from adoptforme_chatbot.conversation import Conversation
from adoptforme_chatbot.llm.base import LLMError, LLMProvider, ToolSpec
from adoptforme_chatbot.mcp_host.logger import InteractionLogger
from adoptforme_chatbot.mcp_host.manager import MCPManager, UnknownToolError
from adoptforme_chatbot.presentation import Presenter

SYSTEM_PROMPT = """\
You are the AdoptForMe assistant, a console agent for an animal shelter. You are \
also a general-purpose assistant: answer ordinary questions directly, without \
tools.

You are connected to several MCP servers. Their tools are the only source of \
truth about shelter animals, files and repositories.

Rules:
* Never invent an animal, an id, an availability status, a file path or a commit. \
If you have not seen it in a tool result in this conversation, you do not know it.
* Prefer one well-chosen tool call over several speculative ones. When you do need \
several independent calls, request them together.
* When you recommend animals, relay the concerns the tool returned, not only the \
reasons. A high score with an unaddressed concern is not a good recommendation.
* Explain the score as a shelter heuristic, never as a guarantee.
* register_adoption is irreversible. Call it only after the user has explicitly \
chosen one animal and given their full name and e-mail; confirm the animal by name \
and id in your reply.
* Filesystem and Git tools act on real files. Say what you changed.
* Keep answers compact and readable in a terminal: short paragraphs, short lists, \
no large tables.
* Reply in the language the user writes in.
"""

COMMANDS = {
    "/help": "Show this list of commands.",
    "/servers": "Configured MCP servers with transport, state, protocol and tool count.",
    "/tools": "Every discovered tool and the server that owns it.",
    "/logs": "Where the logs are, a summary, and the most recent events.",
    "/verbose": "Toggle full tool arguments and results (compact by default).",
    "/clear": "Forget the conversation context (asks for confirmation).",
    "/exit": "Close every MCP client and process, then quit.",
}


class ChatApp:
    """Wires the LLM, the MCP manager, the history and the console together."""

    def __init__(
        self,
        provider: LLMProvider,
        manager: MCPManager,
        interaction_log: InteractionLogger,
        presenter: Presenter,
        max_tool_iterations: int = 8,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.provider = provider
        self.manager = manager
        self.log = interaction_log
        self.ui = presenter
        self.max_tool_iterations = max_tool_iterations
        self.system_prompt = system_prompt
        self.conversation = Conversation()
        self.should_exit = False
        #: Tool calls made during the turn in progress, for the turn footer.
        self.turn_tool_calls = 0

    # ------------------------------------------------------------------ tools

    def tool_specs(self) -> list[ToolSpec]:
        """The tool catalogue advertised to the model, rebuilt from live discovery."""
        return [
            ToolSpec(
                name=reference.exposed_name,
                description=self._describe(reference),
                input_schema=reference.input_schema,
            )
            for reference in self.manager.tools
        ]

    @staticmethod
    def _describe(reference: Any) -> str:
        """Prefix the server name so the model can tell similar tools apart."""
        description = reference.description or "(no description provided by the server)"
        return f"[{reference.server}] {description}"

    # ------------------------------------------------------------------- turn

    async def handle_message(self, user_text: str) -> str:
        """Run one full user turn and return the final assistant text."""
        self.conversation.add_user(user_text)
        tools = self.tool_specs()
        self.turn_tool_calls = 0

        for iteration in range(self.max_tool_iterations):
            try:
                with self.ui.thinking("asking the model"):
                    response = await self.provider.complete(
                        system=self.system_prompt,
                        messages=self.conversation.snapshot(),
                        tools=tools,
                    )
            except LLMError as exc:
                self.ui.error(str(exc))
                return ""

            if not response.wants_tools:
                self.conversation.add_assistant(
                    response.raw_content if response.raw_content is not None else response.text
                )
                return response.text

            # The model narrated what it is about to do; show it before the calls.
            if response.text:
                self.ui.assistant(response.text)
            self.conversation.add_assistant(
                response.raw_content if response.raw_content is not None else response.text
            )

            results: list[tuple[str, str, bool]] = []
            for call in response.tool_calls:
                self.ui.tool_call(call.name, self._owner(call.name), call.arguments)
                try:
                    outcome = await self.manager.call_tool(call.name, call.arguments)
                except UnknownToolError as exc:
                    self.ui.warn(str(exc))
                    results.append((call.id, str(exc), True))
                    continue
                self.ui.tool_result(outcome)
                self.turn_tool_calls += 1
                results.append((call.id, outcome.text, not outcome.ok))

            self.conversation.add_raw(self.provider.tool_result_message(results))

            if iteration == self.max_tool_iterations - 1:
                message = (
                    f"Stopped after {self.max_tool_iterations} tool rounds in a single turn. "
                    "Raise MAX_TOOL_ITERATIONS if this was legitimate."
                )
                self.ui.warn(message)
                return message

        return ""  # pragma: no cover - the loop always returns from inside

    def _owner(self, exposed_name: str) -> str:
        for reference in self.manager.tools:
            if reference.exposed_name == exposed_name:
                return reference.server
        return "?"

    # --------------------------------------------------------------- commands

    async def handle_command(self, line: str) -> bool:
        """Handle a ``/command``. Returns ``True`` when the line was a command."""
        command = line.strip().split()[0].lower()
        if command not in COMMANDS:
            if line.startswith("/"):
                self.ui.warn(f"Unknown command {command}. Type /help.")
                return True
            return False

        if command == "/help":
            self.ui.help(COMMANDS)
            stats = self.conversation.stats()
            self.ui.info(
                f"context: {stats['messages']} messages, {stats['user_turns']} user turns, "
                f"{len(self.manager.tools)} tools from {len(self.manager.connected_servers)} server(s)"
            )
        elif command == "/servers":
            self.ui.servers(self.manager.statuses)
        elif command == "/tools":
            self.ui.tools(self.manager.tools)
        elif command == "/logs":
            self.ui.logs(self.log.summary(), self.log.tail(10))
        elif command == "/verbose":
            self.ui.set_verbose(not self.ui.verbose)
            self.ui.success(
                "Verbose mode on: full tool arguments and results."
                if self.ui.verbose
                else "Compact mode on: arguments and results are abbreviated."
            )
        elif command == "/clear":
            if await self._confirm("Erase the conversation context?"):
                self.conversation.clear()
                self.ui.success("Context cleared. MCP connections are untouched.")
            else:
                self.ui.info("Context kept.")
        elif command == "/exit":
            self.should_exit = True
        return True

    async def _confirm(self, question: str) -> bool:
        answer = await asyncio.to_thread(input, f"{question} [y/N] ")
        return answer.strip().lower() in {"y", "yes", "s", "si", "sí"}

    # ------------------------------------------------------------------- loop

    async def run(self) -> None:
        """Read-evaluate-print until ``/exit``, Ctrl-D or Ctrl-C."""
        while not self.should_exit:
            try:
                line = await asyncio.to_thread(input, self.ui.prompt_text())
            except (EOFError, KeyboardInterrupt):
                self.ui.info("Input closed.")
                return

            line = line.strip()
            if not line:
                continue
            if await self.handle_command(line):
                continue

            started = time.perf_counter()
            try:
                text = await self.handle_message(line)
            except KeyboardInterrupt:
                self.ui.warn("Turn interrupted. The session is still open.")
                continue
            self.ui.assistant(text)
            self.ui.turn_summary(self.turn_tool_calls, int((time.perf_counter() - started) * 1000))
