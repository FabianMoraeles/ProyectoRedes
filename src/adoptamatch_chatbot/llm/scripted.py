"""A scripted provider: a test double, and the engine behind ``--offline``.

It implements the same interface as the Anthropic provider but consumes no API
credits, which is what lets the automated tests exercise the whole host --
routing, logging, multi-step tool loops, context retention -- deterministically.

Two ways to drive it:

* ``ScriptedProvider([...])`` replays a fixed list of :class:`LLMResponse`
  objects, one per ``complete()`` call. This is what the tests use.
* ``ScriptedProvider(responder=fn)`` delegates to a callable that sees the
  history and the tool catalogue. :func:`offline_responder` is a small
  keyword-driven implementation used by ``--offline`` so the chatbot can be
  demonstrated end-to-end without a key.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from adoptamatch_chatbot.llm.base import LLMResponse, ToolCall, ToolSpec

Responder = Callable[[str, list[dict[str, Any]], list[ToolSpec]], LLMResponse]


class ScriptedProvider:
    """Deterministic stand-in for a real model."""

    name = "scripted"

    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        responder: Responder | None = None,
        model: str = "scripted-model",
    ) -> None:
        if responses is None and responder is None:
            raise ValueError("ScriptedProvider needs either a response list or a responder.")
        self.model = model
        self._responses = list(responses or [])
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": list(tools)})
        if self._responses:
            return self._responses.pop(0)
        if self._responder is not None:
            return self._responder(system, messages, tools)
        return LLMResponse(text="(the scripted provider ran out of responses)", stop_reason="end_turn")

    def tool_result_message(self, results: list[tuple[str, str, bool]]) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
                for tool_use_id, content, is_error in results
            ],
        }


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def offline_responder(
    system: str,  # noqa: ARG001 - part of the Responder signature
    messages: list[dict[str, Any]],
    tools: list[ToolSpec],
) -> LLMResponse:
    """A tiny keyword router used by ``--offline``.

    This is **not** a language model. It exists so the MCP plumbing -- discovery,
    routing, logging, the tool loop, clean shutdown -- can be demonstrated and
    tested when no API key is available. It answers by picking one obvious tool
    for a handful of keywords and then reporting the raw result.
    """
    # Only the *current* turn matters: a tool result at the very end means this
    # turn already ran its tool and now needs a closing answer.
    last = messages[-1] if messages else {}
    just_ran_a_tool = isinstance(last.get("content"), list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in last["content"]
    )
    if just_ran_a_tool:
        return LLMResponse(
            text=(
                "Offline mode: the tool result above came straight from the MCP server. "
                "Set ANTHROPIC_API_KEY and drop --offline to get a real answer."
            ),
            stop_reason="end_turn",
        )

    question = _last_user_text(messages).lower()
    available = {tool.name for tool in tools}

    def call(name: str, arguments: dict[str, Any]) -> LLMResponse:
        return LLMResponse(
            text=f"Offline mode: routing this to `{name}`.",
            tool_calls=[ToolCall(id="offline-1", name=name, arguments=arguments)],
            raw_content=[{"type": "text", "text": f"Offline mode: routing this to `{name}`."}],
            stop_reason="tool_use",
        )

    match = re.search(r"\b([aA]\d{3})\b", question)
    if match and "get_animal_details" in available:
        return call("get_animal_details", {"animal_id": match.group(1).upper()})
    if any(word in question for word in ("recommend", "recomienda", "familia", "family")) and (
        "recommend_animals" in available
    ):
        return call(
            "recommend_animals",
            {
                "housing": "apartment",
                "children_at_home": True,
                "resident_dogs": 0,
                "resident_cats": 0,
                "daily_exercise_minutes": 45,
                "experience_level": "none",
                "limit": 3,
            },
        )
    if "search_animals" in available:
        species = "cat" if any(word in question for word in ("cat", "gato")) else "dog"
        return call("search_animals", {"species": species, "limit": 5})

    return LLMResponse(
        text=(
            "Offline mode: no MCP tool matched that request. "
            f"Tools currently available: {', '.join(sorted(available)) or '(none)'}."
        ),
        stop_reason="end_turn",
    )
