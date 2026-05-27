# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for GeminiInference."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.inference import ToolResult, ToolSchema
from ..sdk.gemini import (
    GeminiInference,
    _map_finish_reason,
    _parse_usage,
    _schemas_to_gemini,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_part(text: str, thought: bool = False) -> MagicMock:
    part = MagicMock()
    part.text = text
    part.thought = thought
    part.function_call = None
    part.function_response = None
    return part


def _make_function_call_part(name: str, args: dict[str, Any]) -> MagicMock:
    part = MagicMock()
    part.text = None
    part.thought = False
    part.function_call = MagicMock()
    part.function_call.name = name
    part.function_call.args = args
    part.function_response = None
    return part


def _make_response(
    parts: list[MagicMock],
    finish_reason: str = "STOP",
    prompt_tokens: int = 10,
    output_tokens: int = 20,
) -> MagicMock:
    candidate = MagicMock()
    candidate.finish_reason = finish_reason
    candidate.content = MagicMock()
    candidate.content.parts = parts
    candidate.content.role = "model"

    usage = MagicMock()
    usage.prompt_token_count = prompt_tokens
    usage.candidates_token_count = output_tokens
    usage.cached_content_token_count = 0

    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    response.function_calls = None
    return response


def _make_client() -> MagicMock:
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = AsyncMock()
    client.aio.models.generate_content_stream = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestSchemaConversion:
    def test_converts_single_tool(self) -> None:
        schemas = [
            ToolSchema(
                name="get_weather",
                description="Get the weather",
                parameters={"type": "object", "properties": {"city": {"type": "string"}}},
            )
        ]
        result = _schemas_to_gemini(schemas)
        assert len(result) == 1  # single Tool wrapping all declarations
        decls = result[0].function_declarations
        assert len(decls) == 1
        assert decls[0].name == "get_weather"
        assert decls[0].description == "Get the weather"

    def test_converts_multiple_tools(self) -> None:
        schemas = [
            ToolSchema(name="a", description="A", parameters={}),
            ToolSchema(name="b", description="B", parameters={}),
        ]
        result = _schemas_to_gemini(schemas)
        assert len(result) == 1
        assert len(result[0].function_declarations) == 2


class TestFinishReasonMapping:
    def test_stop(self) -> None:
        assert _map_finish_reason("STOP") == "stop"

    def test_max_tokens(self) -> None:
        assert _map_finish_reason("MAX_TOKENS") == "length"

    def test_safety(self) -> None:
        assert _map_finish_reason("SAFETY") == "stop"

    def test_none(self) -> None:
        assert _map_finish_reason(None) == "stop"


class TestParseUsage:
    def test_parses_usage_metadata(self) -> None:
        meta = MagicMock()
        meta.prompt_token_count = 100
        meta.candidates_token_count = 50
        meta.cached_content_token_count = 10
        usage = _parse_usage(meta)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cached_input_tokens == 10
        assert usage.total_tokens == 150

    def test_handles_none(self) -> None:
        usage = _parse_usage(None)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0


# ---------------------------------------------------------------------------
# Unit tests: protocol methods
# ---------------------------------------------------------------------------


class TestConversationManagement:
    def test_set_system_prompt(self) -> None:
        inf = GeminiInference(_make_client(), model_name="test-model")
        inf.set_system_prompt("You are helpful.")
        assert inf._system_prompt == "You are helpful."

    def test_add_user_message(self) -> None:
        inf = GeminiInference(_make_client(), model_name="test-model")
        inf.add_user_message("Hello")
        assert len(inf._messages) == 1
        assert inf._messages[0].role == "user"

    def test_add_tool_results(self) -> None:
        inf = GeminiInference(_make_client(), model_name="test-model")
        inf.add_tool_results(
            [
                ToolResult(tool_call_id="call_1", content="result", tool_name="my_tool"),
            ]
        )
        assert len(inf._messages) == 1
        assert inf._messages[0].role == "user"

    def test_reset(self) -> None:
        inf = GeminiInference(_make_client(), model_name="test-model")
        inf.add_user_message("Hello")
        inf.reset()
        assert len(inf._messages) == 0

    def test_get_messages_includes_system(self) -> None:
        inf = GeminiInference(_make_client(), model_name="test-model")
        inf.set_system_prompt("System prompt")
        inf.add_user_message("Hello")
        msgs = inf.get_messages()
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_replace_history(self) -> None:
        inf = GeminiInference(_make_client(), model_name="test-model")
        inf.add_user_message("A")
        inf.add_user_message("B")
        inf.replace_history("Summary of A and B")
        assert len(inf._messages) == 1
        assert inf._messages[0].role == "user"


# ---------------------------------------------------------------------------
# Unit tests: complete
# ---------------------------------------------------------------------------


class TestComplete:
    @pytest.mark.asyncio
    async def test_text_response(self) -> None:
        client = _make_client()
        response = _make_response([_make_text_part("Hello!")])
        client.aio.models.generate_content.return_value = response

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Hi")
        result = await inf.complete()

        assert result.text == "Hello!"
        assert result.tool_calls == []
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 20

    @pytest.mark.asyncio
    async def test_tool_call_response(self) -> None:
        client = _make_client()
        response = _make_response(
            [
                _make_function_call_part("get_weather", {"city": "Boston"}),
            ]
        )
        client.aio.models.generate_content.return_value = response

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Weather?")
        result = await inf.complete()

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_weather"
        assert json.loads(result.tool_calls[0].arguments) == {"city": "Boston"}

    @pytest.mark.asyncio
    async def test_thinking_response(self) -> None:
        client = _make_client()
        response = _make_response(
            [
                _make_text_part("Let me think...", thought=True),
                _make_text_part("The answer is 42."),
            ]
        )
        client.aio.models.generate_content.return_value = response

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Think hard")
        result = await inf.complete()

        assert result.thinking == "Let me think..."
        assert result.text == "The answer is 42."

    @pytest.mark.asyncio
    async def test_appends_to_history(self) -> None:
        client = _make_client()
        response = _make_response([_make_text_part("Reply")])
        client.aio.models.generate_content.return_value = response

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Hi")
        await inf.complete()

        # Should have user message + assistant response
        assert len(inf._messages) == 2

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self) -> None:
        client = _make_client()
        response = _make_response([_make_text_part("OK")])
        client.aio.models.generate_content.side_effect = [
            TimeoutError("timeout"),
            response,
        ]

        inf = GeminiInference(client, model_name="test-model", retry_delay=0.01)
        inf.add_user_message("Hi")
        result = await inf.complete()
        assert result.text == "OK"

    @pytest.mark.asyncio
    async def test_passes_tools_in_config(self) -> None:
        client = _make_client()
        response = _make_response([_make_text_part("OK")])
        client.aio.models.generate_content.return_value = response

        tools = [ToolSchema(name="t", description="d", parameters={"type": "object"})]
        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Hi")
        await inf.complete(tools=tools)

        call_kwargs = client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config.tools is not None


# ---------------------------------------------------------------------------
# Unit tests: finish_reason propagation
# ---------------------------------------------------------------------------


class TestFinishReasonPropagation:
    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        client = _make_client()
        response = _make_response([_make_text_part("Done")], finish_reason="STOP")
        client.aio.models.generate_content.return_value = response

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Hi")
        result = await inf.complete()

        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_max_tokens(self) -> None:
        client = _make_client()
        response = _make_response([_make_text_part("truncated")], finish_reason="MAX_TOKENS")
        client.aio.models.generate_content.return_value = response

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("write a long essay")
        result = await inf.complete()

        assert result.finish_reason == "length"

    @pytest.mark.asyncio
    async def test_tool_calls(self) -> None:
        client = _make_client()
        response = _make_response(
            [_make_function_call_part("get_weather", {"city": "NYC"})],
            finish_reason="STOP",
        )
        client.aio.models.generate_content.return_value = response

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Weather?")
        result = await inf.complete()

        assert result.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# Unit tests: PromptTooLongError detection
# ---------------------------------------------------------------------------


class TestPromptTooLongDetection:
    @pytest.mark.asyncio
    async def test_raises_prompt_too_long(self) -> None:
        from core.inference import PromptTooLongError

        try:
            from google.api_core.exceptions import InvalidArgument
        except ImportError:
            pytest.skip("google.api_core not available")

        client = _make_client()
        client.aio.models.generate_content.side_effect = InvalidArgument(
            "Request payload size exceeds the context window limit"
        )

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Hi")

        with pytest.raises(PromptTooLongError):
            await inf.complete()

    @pytest.mark.asyncio
    async def test_reraises_other_invalid_argument(self) -> None:
        try:
            from google.api_core.exceptions import InvalidArgument
        except ImportError:
            pytest.skip("google.api_core not available")

        client = _make_client()
        client.aio.models.generate_content.side_effect = InvalidArgument("Invalid model name")

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Hi")

        with pytest.raises(InvalidArgument):
            await inf.complete()


# ---------------------------------------------------------------------------
# Unit tests: stream
# ---------------------------------------------------------------------------


class TestStream:
    @pytest.mark.asyncio
    async def test_streams_text(self) -> None:
        client = _make_client()

        chunk1 = _make_response([_make_text_part("Hello ")])
        chunk1.usage_metadata = None
        chunk2 = _make_response([_make_text_part("world!")])

        async def fake_stream():
            yield chunk1
            yield chunk2

        client.aio.models.generate_content_stream.return_value = fake_stream()

        inf = GeminiInference(client, model_name="test-model")
        inf.add_user_message("Hi")

        events = []
        async for event in inf.stream():
            events.append(event)

        # Text events + final event
        text_events = [e for e in events if e.delta]
        assert len(text_events) == 2
        assert text_events[0].delta == "Hello "
        assert text_events[1].delta == "world!"

        # Final event has usage
        final = events[-1]
        assert final.usage is not None
        assert final.finish_reason == "stop"
