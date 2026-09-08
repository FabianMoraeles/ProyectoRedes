"""Anthropic implementation of :class:`~adoptforme_chatbot.llm.base.LLMProvider`.

Uses the official ``anthropic`` SDK (1.x) and the Messages API. The agentic loop
itself lives in :mod:`adoptforme_chatbot.app`: this class is a single-turn
adapter, because the host -- not the SDK -- must own tool routing so that every
call goes through the MCP manager and the interaction log.

The model id is never hard-coded in application logic; it arrives from
``ANTHROPIC_MODEL`` via :class:`~adoptforme_chatbot.config.AppConfig`.
"""

from __future__ import annotations

from typing import Any

import anthropic

from adoptforme_chatbot.llm.base import LLMError, LLMResponse, ToolCall, ToolSpec

#: Generous but bounded: a recommendation with six explained candidates is long.
MAX_TOKENS = 8000


class AnthropicProvider:
    """Talks to the Claude Messages API."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout_seconds: float = 120.0) -> None:
        if not api_key:
            raise LLMError("An Anthropic API key is required. Set ANTHROPIC_API_KEY.")
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        payload = [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in tools
        ]
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,  # type: ignore[arg-type]
                tools=payload,  # type: ignore[arg-type]
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in your .env.") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(
                f"The model '{self.model}' was not found. Check ANTHROPIC_MODEL in your .env."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic rate limit reached. Wait a moment and try again.") from exc
        except anthropic.BadRequestError as exc:
            # An empty balance is by far the most common 400 for a new account, and
            # the generic "HTTP 400" below tells the user nothing useful about it.
            if "credit balance" in (exc.message or "").lower():
                raise LLMError(
                    "The API key is valid, but the account has no credit balance. "
                    "Open https://console.anthropic.com/ -> Plans & Billing to claim the "
                    "free trial credit or add credits. Meanwhile, --offline exercises "
                    "everything except the model."
                ) from exc
            raise LLMError(f"Anthropic rejected the request: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic returned HTTP {exc.status_code}: {exc.message}") from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(arguments)))

        return LLMResponse(
            text="\n".join(part for part in text_parts if part).strip(),
            tool_calls=tool_calls,
            raw_content=response.content,
            stop_reason=response.stop_reason,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )

    def tool_result_message(self, results: list[tuple[str, str, bool]]) -> dict[str, Any]:
        """All results for one assistant turn go back in a *single* user message.

        Splitting them across several messages teaches the model to stop
        requesting parallel tool calls.
        """
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
