"""Google Gemini implementation of :class:`~adoptamatch_chatbot.llm.base.LLMProvider`.

The free alternative to Anthropic: the assignment only asks for "connection to an
LLM at the API level", not a specific vendor, and a fresh Gemini API key from
https://aistudio.google.com/apikey carries a genuine free tier with tool calling
-- unlike OpenAI's API, which has no free tier at all.

Like :class:`~adoptamatch_chatbot.llm.anthropic_provider.AnthropicProvider`, this
is a single-turn adapter: the agentic loop and tool routing stay in
:mod:`adoptamatch_chatbot.app`, so the model never executes a tool by itself.
Automatic function calling is explicitly disabled for that reason.

Gemini has no equivalent of Anthropic's per-call ``tool_use_id``: a
``function_call`` part carries only a name. The host still needs a stable id to
pair a call with its result (:meth:`ChatApp.handle_message` threads it through
unchanged), so one is minted here per call and mapped back to the name when the
matching ``function_response`` is built.
"""

from __future__ import annotations

from typing import Any

from google import genai
from google.genai import errors, types

from adoptamatch_chatbot.llm.base import LLMError, LLMResponse, ToolCall, ToolSpec

#: Mirrors the Anthropic provider's budget; a long, multi-candidate explanation
#: needs headroom.
MAX_OUTPUT_TOKENS = 8000

RATE_LIMIT_HINT = (
    "Gemini's free-tier rate limit was reached (requests per minute or per day). "
    "Wait a moment and try again -- see https://ai.google.dev/gemini-api/docs/rate-limits."
)


class GeminiProvider:
    """Talks to the Gemini API through the ``google-genai`` SDK."""

    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout_seconds: float = 120.0) -> None:
        if not api_key:
            raise LLMError(
                "A Gemini API key is required. Get a free one at "
                "https://aistudio.google.com/apikey and set GEMINI_API_KEY."
            )
        self.model = model
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )
        self._call_names: dict[str, str] = {}
        self._next_call_id = 0

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        contents = [self._to_content(message) for message in messages]
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        if tools:
            # MCP tool schemas are already JSON Schema (built by pydantic in
            # minimcp.py), and parameters_json_schema accepts that directly --
            # no translation to Gemini's older, restricted Schema type needed.
            declarations = [
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=tool.input_schema,
                )
                for tool in tools
            ]
            config.tools = [types.Tool(function_declarations=declarations)]
            config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)

        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except errors.ClientError as exc:
            raise LLMError(self._explain_client_error(exc)) from exc
        except errors.ServerError as exc:
            raise LLMError(f"Gemini's servers had a problem (HTTP {exc.code}): {exc.message}") from exc
        except errors.APIError as exc:
            raise LLMError(f"Gemini returned HTTP {exc.code}: {exc.message}") from exc

        candidates = response.candidates or []
        if not candidates:
            reason = getattr(response.prompt_feedback, "block_reason", None)
            raise LLMError(f"Gemini returned no candidate (prompt blocked: {reason}).")
        candidate = candidates[0]
        parts = candidate.content.parts if candidate.content else []

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_blocks: list[dict[str, Any]] = []
        for part in parts or []:
            if part.text:
                text_parts.append(part.text)
                raw_blocks.append({"type": "text", "text": part.text})
            elif part.function_call:
                call_id = self._mint_call_id(part.function_call.name)
                arguments = dict(part.function_call.args or {})
                tool_calls.append(ToolCall(id=call_id, name=part.function_call.name, arguments=arguments))
                raw_blocks.append(
                    {"type": "tool_use", "id": call_id, "name": part.function_call.name, "input": arguments}
                )

        usage = response.usage_metadata
        return LLMResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            raw_content=raw_blocks,
            stop_reason=candidate.finish_reason.value if candidate.finish_reason else None,
            usage=(
                {
                    "input_tokens": usage.prompt_token_count or 0,
                    "output_tokens": usage.candidates_token_count or 0,
                }
                if usage
                else {}
            ),
        )

    def tool_result_message(self, results: list[tuple[str, str, bool]]) -> dict[str, Any]:
        """Same envelope :class:`AnthropicProvider` uses; translated in ``_to_content``."""
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}
                for call_id, content, is_error in results
            ],
        }

    # ------------------------------------------------------------- translation

    def _to_content(self, message: dict[str, Any]) -> types.Content:
        role = message["role"]
        content = message.get("content")
        if role == "user" and isinstance(content, list):
            # A tool-result envelope built by tool_result_message.
            return types.Content(role="user", parts=[self._result_part(block) for block in content])
        if role == "user":
            return types.Content(role="user", parts=[types.Part(text=content)])
        if role == "assistant":
            return types.Content(role="model", parts=self._blocks_to_parts(content))
        raise LLMError(f"Cannot translate message for Gemini: unexpected shape {message!r}")

    def _blocks_to_parts(self, content: Any) -> list[types.Part]:
        if isinstance(content, str):
            return [types.Part(text=content)]
        parts: list[types.Part] = []
        for block in content or []:
            if block.get("type") == "text":
                parts.append(types.Part(text=block["text"]))
            elif block.get("type") == "tool_use":
                call = types.FunctionCall(name=block["name"], args=block["input"])
                parts.append(types.Part(function_call=call))
        return parts

    def _result_part(self, block: dict[str, Any]) -> types.Part:
        name = self._call_names.get(block["tool_use_id"], block["tool_use_id"])
        payload = {"error": block["content"]} if block.get("is_error") else {"result": block["content"]}
        return types.Part.from_function_response(name=name, response=payload)

    def _mint_call_id(self, name: str) -> str:
        self._next_call_id += 1
        call_id = f"call_{self._next_call_id}"
        self._call_names[call_id] = name
        return call_id

    @staticmethod
    def _explain_client_error(exc: errors.ClientError) -> str:
        message = (exc.message or "").lower()
        if exc.code in (401, 403):
            return "Gemini rejected the API key. Check GEMINI_API_KEY in your .env."
        if exc.code == 429 or "resource_exhausted" in message or "quota" in message:
            return RATE_LIMIT_HINT
        if exc.code == 404:
            return f"The model '{exc.message}' was not found. Check GEMINI_MODEL in your .env."
        return f"Gemini rejected the request (HTTP {exc.code}): {exc.message}"
