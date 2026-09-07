"""The Gemini provider's own logic: message translation and error mapping.

The SDK call itself is not exercised here (that would need a real key and
network access); what is worth testing is the part this project actually wrote:
translating the host's provider-neutral history into Gemini's ``Content``/
``Part`` shapes, minting call ids Gemini does not provide, and turning the
SDK's typed errors into messages a student can act on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import errors

from adoptamatch_chatbot.llm.base import LLMError, ToolSpec
from adoptamatch_chatbot.llm.gemini_provider import GeminiProvider

TEXT_ONLY_RESPONSE = SimpleNamespace(
    candidates=[
        SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="hi", function_call=None)]),
            finish_reason=SimpleNamespace(value="STOP"),
        )
    ],
    usage_metadata=None,
)


def make_provider() -> GeminiProvider:
    return GeminiProvider(api_key="fake-key-for-tests", model="gemini-flash-latest")


class TestConstruction:
    def test_a_missing_api_key_is_rejected_before_any_network_call(self) -> None:
        with pytest.raises(LLMError, match="GEMINI_API_KEY"):
            GeminiProvider(api_key="", model="gemini-flash-latest")


class TestOutgoingTranslation:
    def test_a_plain_user_turn_becomes_a_single_text_part(self) -> None:
        provider = make_provider()
        content = provider._to_content({"role": "user", "content": "hola"})
        assert content.role == "user"
        assert content.parts[0].text == "hola"

    def test_an_assistant_text_turn_becomes_role_model(self) -> None:
        provider = make_provider()
        content = provider._to_content({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        assert content.role == "model"
        assert content.parts[0].text == "ok"

    def test_an_assistant_tool_use_turn_becomes_a_function_call_part(self) -> None:
        provider = make_provider()
        content = provider._to_content(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "search_animals",
                        "input": {"species": "dog"},
                    }
                ],
            }
        )
        assert content.role == "model"
        call = content.parts[0].function_call
        assert call.name == "search_animals"
        assert call.args == {"species": "dog"}

    def test_a_tool_result_envelope_round_trips_through_the_call_name_map(self) -> None:
        """The host addresses results by the id minted in complete(); Gemini needs the name."""
        provider = make_provider()
        provider._call_names["call_1"] = "search_animals"
        message = provider.tool_result_message([("call_1", '{"count": 2}', False)])
        content = provider._to_content(message)
        assert content.role == "user"
        response_part = content.parts[0].function_response
        assert response_part.name == "search_animals"
        assert response_part.response == {"result": '{"count": 2}'}

    def test_a_failed_tool_result_is_marked_as_an_error(self) -> None:
        provider = make_provider()
        provider._call_names["call_1"] = "search_animals"
        message = provider.tool_result_message([("call_1", "boom", True)])
        content = provider._to_content(message)
        assert content.parts[0].function_response.response == {"error": "boom"}

    def test_an_unrecognised_message_shape_is_a_clear_error_not_a_crash(self) -> None:
        provider = make_provider()
        with pytest.raises(LLMError, match="Cannot translate"):
            provider._to_content({"role": "system", "content": "???"})


class TestIncomingTranslation:
    @pytest.mark.asyncio
    async def test_a_text_only_reply_produces_no_tool_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = make_provider()
        fake_response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[SimpleNamespace(text="hello", function_call=None)]),
                    finish_reason=SimpleNamespace(value="STOP"),
                )
            ],
            usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5),
        )

        async def fake_generate_content(**kwargs: object) -> SimpleNamespace:
            return fake_response

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate_content)

        result = await provider.complete(
            system="be helpful", messages=[{"role": "user", "content": "hi"}], tools=[]
        )
        assert result.text == "hello"
        assert result.tool_calls == []
        assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    @pytest.mark.asyncio
    async def test_a_function_call_reply_mints_an_id_the_host_can_pair_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = make_provider()
        call_part = SimpleNamespace(
            text=None,
            function_call=SimpleNamespace(name="search_animals", args={"species": "cat"}),
            thought_signature=b"opaque-signature",
        )
        fake_response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[call_part]),
                    finish_reason=SimpleNamespace(value="STOP"),
                )
            ],
            usage_metadata=None,
        )

        async def fake_generate_content(**kwargs: object) -> SimpleNamespace:
            return fake_response

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate_content)

        result = await provider.complete(
            system="be helpful",
            messages=[{"role": "user", "content": "find a cat"}],
            tools=[ToolSpec(name="search_animals", description="search", input_schema={"type": "object"})],
        )
        assert result.wants_tools
        call = result.tool_calls[0]
        assert call.name == "search_animals"
        assert call.arguments == {"species": "cat"}
        # The id must resolve back to the same name for the reply.
        assert provider._call_names[call.id] == "search_animals"
        # The thought signature must be carried into raw_content, or Gemini's
        # thinking models reject the next request when this turn is echoed back.
        assert result.raw_content[0]["thought_signature"] == b"opaque-signature"

    @pytest.mark.asyncio
    async def test_a_tool_use_block_replays_its_thought_signature_on_the_next_turn(self) -> None:
        provider = make_provider()
        message = {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "search_animals",
                    "input": {"species": "cat"},
                    "thought_signature": b"opaque-signature",
                }
            ],
        }
        content = provider._to_content(message)
        assert content.parts[0].thought_signature == b"opaque-signature"

    @pytest.mark.asyncio
    async def test_no_candidates_is_reported_as_an_llm_error_not_an_index_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = make_provider()
        fake_response = SimpleNamespace(candidates=[], prompt_feedback=SimpleNamespace(block_reason="SAFETY"))

        async def fake_generate_content(**kwargs: object) -> SimpleNamespace:
            return fake_response

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate_content)

        with pytest.raises(LLMError, match="SAFETY"):
            await provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])


class TestErrorMapping:
    def _client_error(self, code: int, message: str) -> errors.ClientError:
        return errors.ClientError(code=code, response_json={"error": {"code": code, "message": message}})

    def test_401_is_explained_as_a_bad_key(self) -> None:
        provider = make_provider()
        explained = provider._explain_client_error(self._client_error(401, "invalid api key"))
        assert "GEMINI_API_KEY" in explained

    def test_429_is_explained_as_the_free_tier_rate_limit(self) -> None:
        provider = make_provider()
        explained = provider._explain_client_error(self._client_error(429, "quota exceeded"))
        assert "rate limit" in explained.lower()

    def test_404_names_the_model_env_var(self) -> None:
        provider = make_provider()
        explained = provider._explain_client_error(self._client_error(404, "model not found"))
        assert "GEMINI_MODEL" in explained

    @pytest.mark.asyncio
    async def test_a_client_error_raised_by_the_sdk_surfaces_as_an_llm_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = make_provider()

        async def fake_generate_content(**kwargs: object) -> None:
            raise self._client_error(429, "quota exceeded")

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate_content)

        with pytest.raises(LLMError, match="rate limit"):
            await provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])


class TestRetryOn503:
    """A 503 is Gemini's free-tier way of saying "try again shortly"; it is the
    single most common failure hit while building this provider, so it is worth
    absorbing with a bounded, backed-off retry instead of failing the turn.
    """

    def _server_error(self, code: int, message: str) -> errors.ServerError:
        return errors.ServerError(code=code, response_json={"error": {"code": code, "message": message}})

    def _patch_sleep(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        recorded: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            recorded.append(seconds)

        monkeypatch.setattr("adoptamatch_chatbot.llm.gemini_provider.asyncio.sleep", fake_sleep)
        return recorded

    @pytest.mark.asyncio
    async def test_a_503_is_retried_with_backoff_and_can_still_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = make_provider()
        sleeps = self._patch_sleep(monkeypatch)
        attempts = {"n": 0}

        async def fake_generate_content(**kwargs: object) -> SimpleNamespace:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise self._server_error(503, "overloaded")
            return TEXT_ONLY_RESPONSE

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate_content)

        result = await provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
        assert result.text == "hi"
        assert attempts["n"] == 3
        assert sleeps == [1.0, 2.0]  # exponential backoff, not hammering the API

    @pytest.mark.asyncio
    async def test_503_gives_up_after_the_retry_budget_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = make_provider()
        self._patch_sleep(monkeypatch)

        async def fake_generate_content(**kwargs: object) -> None:
            raise self._server_error(503, "overloaded")

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate_content)

        with pytest.raises(LLMError, match="503"):
            await provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])

    @pytest.mark.asyncio
    async def test_a_non_503_server_error_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Retrying a plain 500 would just delay the same failure."""
        provider = make_provider()
        attempts = {"n": 0}

        async def fake_generate_content(**kwargs: object) -> None:
            attempts["n"] += 1
            raise self._server_error(500, "internal error")

        monkeypatch.setattr(provider._client.aio.models, "generate_content", fake_generate_content)

        with pytest.raises(LLMError, match="500"):
            await provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
        assert attempts["n"] == 1
