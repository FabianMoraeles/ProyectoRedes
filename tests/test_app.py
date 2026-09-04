"""The conversation loop: tool rounds, context retention and console commands.

Every model turn is scripted, so these tests are deterministic and cost nothing.
The MCP servers, however, are real subprocesses.
"""

from __future__ import annotations

import json

import pytest
from rich.console import Console

from adoptamatch_chatbot.app import COMMANDS, ChatApp
from adoptamatch_chatbot.conversation import Conversation
from adoptamatch_chatbot.llm.base import LLMError, LLMResponse, ToolCall
from adoptamatch_chatbot.llm.scripted import ScriptedProvider
from adoptamatch_chatbot.mcp_host.logger import InteractionLogger
from adoptamatch_chatbot.mcp_host.manager import MCPManager
from adoptamatch_chatbot.presentation import Presenter


def quiet_presenter() -> Presenter:
    """A presenter writing to an in-memory console, so tests print nothing."""
    return Presenter(
        Console(
            file=open(  # noqa: SIM115 - closed with the process
                __import__("os").devnull, "w", encoding="utf-8"
            ),
            width=100,
        )
    )


def build_app(provider: ScriptedProvider, manager: MCPManager, log: InteractionLogger) -> ChatApp:
    return ChatApp(
        provider=provider,
        manager=manager,
        interaction_log=log,
        presenter=quiet_presenter(),
        max_tool_iterations=5,
    )


def say(text: str) -> LLMResponse:
    return LLMResponse(text=text, raw_content=[{"type": "text", "text": text}], stop_reason="end_turn")


def call(*calls: tuple[str, dict]) -> LLMResponse:
    tool_calls = [ToolCall(id=f"call-{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)]
    return LLMResponse(
        text="",
        tool_calls=tool_calls,
        raw_content=[
            {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments} for tc in tool_calls
        ],
        stop_reason="tool_use",
    )


# ---------------------------------------------------------------- tool catalogue


async def test_tool_catalogue_is_built_from_live_discovery(
    duo_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    app = build_app(ScriptedProvider([say("hi")]), duo_manager, interaction_log)
    specs = {spec.name for spec in app.tool_specs()}
    assert "alpha__ping" in specs and "beta__ping" in specs
    assert all(spec.input_schema["type"] == "object" for spec in app.tool_specs())


async def test_tool_descriptions_name_their_server(
    duo_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    app = build_app(ScriptedProvider([say("hi")]), duo_manager, interaction_log)
    spec = next(spec for spec in app.tool_specs() if spec.name == "beta__ping")
    assert spec.description.startswith("[beta]")


# --------------------------------------------------------------------- one turn


async def test_a_plain_question_never_touches_a_tool(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    provider = ScriptedProvider([say("The capital of France is Paris.")])
    app = build_app(provider, alpha_manager, interaction_log)
    answer = await app.handle_message("What is the capital of France?")
    assert answer == "The capital of France is Paris."
    assert interaction_log.summary()["tool_calls"] == 0


async def test_single_tool_round_trip(alpha_manager: MCPManager, interaction_log: InteractionLogger) -> None:
    provider = ScriptedProvider([call(("add", {"a": 2, "b": 40})), say("The answer is 42.")])
    app = build_app(provider, alpha_manager, interaction_log)
    answer = await app.handle_message("Add 2 and 40.")

    assert answer == "The answer is 42."
    assert interaction_log.summary()["tool_calls"] == 1
    # user, assistant(tool_use), user(tool_result), assistant(text)
    assert len(app.conversation.messages) == 4
    tool_result_message = app.conversation.messages[2]
    assert tool_result_message["content"][0]["type"] == "tool_result"
    assert json.loads(tool_result_message["content"][0]["content"])["result"] == 42


async def test_parallel_tool_calls_return_in_one_message(
    duo_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    provider = ScriptedProvider(
        [
            call(("alpha__ping", {"name": "Ana"}), ("beta__ping", {"name": "Ana"})),
            say("Both servers answered."),
        ]
    )
    app = build_app(provider, duo_manager, interaction_log)
    await app.handle_message("Ping both servers.")

    results = app.conversation.messages[2]["content"]
    assert len(results) == 2, "all results for one turn must go back in a single user message"
    assert {block["tool_use_id"] for block in results} == {"call-0", "call-1"}
    assert interaction_log.summary()["tool_calls"] == 2


async def test_several_sequential_tool_rounds(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    provider = ScriptedProvider(
        [
            call(("add", {"a": 1, "b": 1})),
            call(("add", {"a": 2, "b": 2})),
            say("Done: 2 and 4."),
        ]
    )
    app = build_app(provider, alpha_manager, interaction_log)
    answer = await app.handle_message("Add twice.")
    assert answer == "Done: 2 and 4."
    assert interaction_log.summary()["tool_calls"] == 2


async def test_iteration_cap_stops_a_runaway_loop(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    provider = ScriptedProvider([call(("add", {"a": 1, "b": 1})) for _ in range(20)])
    app = build_app(provider, alpha_manager, interaction_log)
    answer = await app.handle_message("Loop forever.")
    assert "Stopped after 5 tool rounds" in answer
    assert interaction_log.summary()["tool_calls"] == 5


# ---------------------------------------------------------------- error paths


async def test_a_failing_tool_is_reported_back_to_the_model(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    provider = ScriptedProvider([call(("boom", {})), say("The tool refused; here is why.")])
    app = build_app(provider, alpha_manager, interaction_log)
    answer = await app.handle_message("Trigger the failing tool.")

    block = app.conversation.messages[2]["content"][0]
    assert block["is_error"] is True
    assert "refused on purpose" in block["content"]
    assert answer == "The tool refused; here is why."


async def test_a_hallucinated_tool_name_does_not_end_the_turn(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    provider = ScriptedProvider([call(("teleport", {})), say("That tool does not exist.")])
    app = build_app(provider, alpha_manager, interaction_log)
    answer = await app.handle_message("Use the teleport tool.")

    block = app.conversation.messages[2]["content"][0]
    assert block["is_error"] is True
    assert "No connected MCP server exposes" in block["content"]
    assert answer == "That tool does not exist."


async def test_an_llm_failure_is_reported_without_killing_the_session(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    class Failing(ScriptedProvider):
        async def complete(self, **kwargs):  # type: ignore[override]
            raise LLMError("the provider is down")

    app = build_app(Failing([say("unused")]), alpha_manager, interaction_log)
    assert await app.handle_message("hello") == ""


# ------------------------------------------------------------ context retention


async def test_the_second_question_still_sees_the_first(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    """The core context requirement: a follow-up resolves against earlier turns."""
    provider = ScriptedProvider(
        [
            say("Luna is a two-year-old Border Collie mix."),
            say("She needs 90 minutes of exercise a day."),
        ]
    )
    app = build_app(provider, alpha_manager, interaction_log)
    await app.handle_message("Tell me about Luna.")
    await app.handle_message("And how much exercise does she need?")

    second_request = provider.calls[1]["messages"]
    rendered = json.dumps(second_request, default=str)
    assert "Tell me about Luna." in rendered
    assert "Border Collie" in rendered, "the first answer must still be in the payload"
    assert second_request[-1]["content"] == "And how much exercise does she need?"


async def test_tool_results_stay_in_context_for_the_next_question(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    provider = ScriptedProvider(
        [call(("add", {"a": 20, "b": 22})), say("42."), say("Yes, the same 42 as before.")]
    )
    app = build_app(provider, alpha_manager, interaction_log)
    await app.handle_message("Add 20 and 22.")
    await app.handle_message("Is that still the number?")

    rendered = json.dumps(provider.calls[2]["messages"], default=str)
    assert '"result": 42' in rendered or "42" in rendered
    assert "tool_result" in rendered


async def test_clearing_the_context_drops_the_history_only(
    alpha_manager: MCPManager, interaction_log: InteractionLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([say("first"), say("second")])
    app = build_app(provider, alpha_manager, interaction_log)
    await app.handle_message("hello")
    assert app.conversation.messages

    monkeypatch.setattr(ChatApp, "_confirm", lambda self, question: _yes())
    assert await app.handle_command("/clear") is True
    assert app.conversation.messages == []
    assert app.manager.connected_servers == ["alpha"], "clearing context must not drop connections"


async def _yes() -> bool:
    return True


def test_history_trimming_never_orphans_a_tool_result() -> None:
    conversation = Conversation(max_messages=4)
    conversation.add_user("q1")
    conversation.add_assistant([{"type": "tool_use", "id": "1", "name": "t", "input": {}}])
    conversation.add_raw({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1"}]})
    conversation.add_user("q2")
    conversation.add_assistant("a2")
    conversation.add_user("q3")

    assert len(conversation.messages) <= 4
    assert not Conversation._is_tool_result(conversation.messages[0])


# -------------------------------------------------------------------- commands


async def test_every_documented_command_is_handled(
    alpha_manager: MCPManager, interaction_log: InteractionLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(ScriptedProvider([say("unused")]), alpha_manager, interaction_log)
    monkeypatch.setattr(ChatApp, "_confirm", lambda self, question: _yes())
    for command in COMMANDS:
        assert await app.handle_command(command) is True
    assert app.should_exit is True  # /exit was in the list


async def test_ordinary_text_is_not_treated_as_a_command(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    app = build_app(ScriptedProvider([say("unused")]), alpha_manager, interaction_log)
    assert await app.handle_command("hello there") is False


async def test_unknown_slash_command_is_rejected_without_reaching_the_model(
    alpha_manager: MCPManager, interaction_log: InteractionLogger
) -> None:
    provider = ScriptedProvider([say("should not be used")])
    app = build_app(provider, alpha_manager, interaction_log)
    assert await app.handle_command("/nonsense") is True
    assert provider.calls == []
