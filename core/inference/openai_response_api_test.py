# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for OpenAIResponseInference — schema conversion, complete, stream, state."""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.inference import ToolCall, ToolResult, ToolSchema
from .openai_response_api import (
    OpenAIResponseInference,
    _schemas_to_response_api,
    _tool_results_to_input_items,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_obj(**kwargs: Any) -> types.SimpleNamespace:
    """Create a SimpleNamespace (attribute-style object) from kwargs."""
    return types.SimpleNamespace(**kwargs)


def _mock_client() -> AsyncMock:
    """Create a mock AsyncOpenAI client with responses.create."""
    client = AsyncMock(spec=["responses"])
    client.responses = MagicMock()
    client.responses.create = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------


class TestSchemaConversion:
    """Tests for _schemas_to_response_api (flat format)."""

    def test_converts_single_tool(self):
        schemas = [ToolSchema(name="read_file", description="Read a file", parameters={"type": "object"})]
        result = _schemas_to_response_api(schemas)
        assert len(result) == 1
        assert result[0] == {
            "type": "function",
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object"},
        }

    def test_flat_format_no_function_wrapper(self):
        """Response API uses flat format — no nested 'function' key."""
        schemas = [ToolSchema(name="t", description="d", parameters={})]
        result = _schemas_to_response_api(schemas)
        assert "function" not in result[0]
        assert result[0]["name"] == "t"

    def test_converts_multiple_tools(self):
        schemas = [
            ToolSchema(name="a", description="da", parameters={"type": "object"}),
            ToolSchema(name="b", description="db", parameters={"type": "object", "properties": {"x": {"type": "int"}}}),
        ]
        result = _schemas_to_response_api(schemas)
        assert len(result) == 2
        assert result[0]["name"] == "a"
        assert result[1]["name"] == "b"
        assert result[1]["parameters"]["properties"]["x"]["type"] == "int"


class TestToolResultConversion:
    """Tests for _tool_results_to_input_items."""

    def test_converts_results(self):
        results = [
            ToolResult(tool_call_id="call_1", content="file contents here"),
            ToolResult(tool_call_id="call_2", content="error", is_error=True),
        ]
        items = _tool_results_to_input_items(results)
        assert len(items) == 2
        assert items[0] == {"type": "function_call_output", "call_id": "call_1", "output": "file contents here"}
        assert items[1] == {"type": "function_call_output", "call_id": "call_2", "output": "error"}


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


class TestComplete:
    """Tests for complete() with mocked client.responses.create."""

    @pytest.mark.asyncio
    async def test_text_response(self):
        client = _mock_client()
        text_block = _make_obj(type="output_text", text="Hello, world!")
        message_item = _make_obj(
            type="message",
            content=[text_block],
            role="assistant",
            status="completed",
            model_dump=lambda: {"type": "message", "content": [{"type": "output_text", "text": "Hello, world!"}]},
        )
        usage = _make_obj(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            input_tokens_details=_make_obj(cached_tokens=3),
        )
        response = _make_obj(
            output=[message_item],
            usage=usage,
            model="gpt-4.1",
            id="resp_123",
        )
        client.responses.create.return_value = response

        inference = OpenAIResponseInference(client, model_name="test-model")
        inference.set_system_prompt("Be helpful.")
        inference.add_user_message("Hi")

        result = await inference.complete()

        assert result.text == "Hello, world!"
        assert result.tool_calls == []
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5
        assert result.usage.cached_input_tokens == 3
        assert result.model == "gpt-4.1"
        assert result.call_id == "resp_123"

        # Verify instructions was passed
        call_kwargs = client.responses.create.call_args
        assert (
            call_kwargs.kwargs.get("instructions") == "Be helpful."
            or call_kwargs[1].get("instructions") == "Be helpful."
        )

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        client = _mock_client()
        fc_item = _make_obj(
            type="function_call",
            call_id="call_abc",
            name="read_file",
            arguments='{"path": "/tmp/test.txt"}',
            model_dump=lambda: {
                "type": "function_call",
                "call_id": "call_abc",
                "name": "read_file",
                "arguments": '{"path": "/tmp/test.txt"}',
            },
        )
        usage = _make_obj(
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            input_tokens_details=_make_obj(cached_tokens=0),
        )
        response = _make_obj(output=[fc_item], usage=usage, model="gpt-4.1", id="resp_456")
        client.responses.create.return_value = response

        inference = OpenAIResponseInference(client, model_name="test-model")
        inference.add_user_message("Read the file")

        result = await inference.complete(
            tools=[ToolSchema(name="read_file", description="Read", parameters={"type": "object"})],
        )

        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert isinstance(tc, ToolCall)
        assert tc.id == "call_abc"
        assert tc.name == "read_file"
        assert tc.arguments == '{"path": "/tmp/test.txt"}'

    @pytest.mark.asyncio
    async def test_finish_reason_stop(self):
        """complete() sets finish_reason='stop' for normal text response."""
        client = _mock_client()
        text_block = _make_obj(type="output_text", text="Hello")
        message_item = _make_obj(
            type="message",
            content=[text_block],
            role="assistant",
            status="completed",
            model_dump=lambda: {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]},
        )
        usage = _make_obj(input_tokens=5, output_tokens=2, total_tokens=7, input_tokens_details=None)
        response = _make_obj(output=[message_item], usage=usage, model="gpt-4.1", id="resp_1", status="completed")
        client.responses.create.return_value = response

        inference = OpenAIResponseInference(client, model_name="test-model")
        inference.add_user_message("Hi")
        result = await inference.complete()

        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_finish_reason_tool_calls(self):
        """complete() sets finish_reason='tool_calls' for tool call response."""
        client = _mock_client()
        fc_item = _make_obj(
            type="function_call",
            call_id="call_1",
            name="read_file",
            arguments='{"path": "/tmp"}',
            model_dump=lambda: {
                "type": "function_call",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path": "/tmp"}',
            },
        )
        usage = _make_obj(input_tokens=5, output_tokens=5, total_tokens=10, input_tokens_details=None)
        response = _make_obj(output=[fc_item], usage=usage, model="gpt-4.1", id="resp_2", status="completed")
        client.responses.create.return_value = response

        inference = OpenAIResponseInference(client, model_name="test-model")
        inference.add_user_message("Read file")
        result = await inference.complete()

        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_finish_reason_length_on_incomplete(self):
        """complete() sets finish_reason='length' when response.status is 'incomplete'."""
        client = _mock_client()
        text_block = _make_obj(type="output_text", text="truncated text")
        message_item = _make_obj(
            type="message",
            content=[text_block],
            role="assistant",
            status="incomplete",
            model_dump=lambda: {"type": "message", "content": [{"type": "output_text", "text": "truncated text"}]},
        )
        usage = _make_obj(input_tokens=5, output_tokens=100, total_tokens=105, input_tokens_details=None)
        response = _make_obj(output=[message_item], usage=usage, model="gpt-4.1", id="resp_3", status="incomplete")
        client.responses.create.return_value = response

        inference = OpenAIResponseInference(client, model_name="test-model")
        inference.add_user_message("Write a long essay")
        result = await inference.complete()

        assert result.finish_reason == "length"
        assert result.text == "truncated text"

    @pytest.mark.asyncio
    async def test_mixed_text_and_tool_calls(self):
        client = _mock_client()
        text_block = _make_obj(type="output_text", text="Let me read that.")
        message_item = _make_obj(
            type="message",
            content=[text_block],
            model_dump=lambda: {"type": "message", "content": [{"type": "output_text", "text": "Let me read that."}]},
        )
        fc_item = _make_obj(
            type="function_call",
            call_id="call_1",
            name="read_file",
            arguments="{}",
            model_dump=lambda: {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "{}"},
        )
        usage = _make_obj(input_tokens=5, output_tokens=5, total_tokens=10, input_tokens_details=None)
        response = _make_obj(output=[message_item, fc_item], usage=usage, model="gpt-4.1", id="resp_789")
        client.responses.create.return_value = response

        inference = OpenAIResponseInference(client, model_name="test-model")
        inference.add_user_message("Read")
        result = await inference.complete()

        assert result.text == "Let me read that."
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"


# ---------------------------------------------------------------------------
# PromptTooLongError detection
# ---------------------------------------------------------------------------


class TestPromptTooLong:
    @pytest.mark.asyncio
    async def test_raises_prompt_too_long(self):
        from openai import BadRequestError

        from core.inference import PromptTooLongError

        client = _mock_client()
        client.responses.create = AsyncMock(
            side_effect=BadRequestError(
                message="This model's maximum context length is 128000 tokens.",
                response=MagicMock(status_code=400),
                body={"error": {"message": "maximum context length"}},
            )
        )

        inference = OpenAIResponseInference(client, model_name="test-model")
        inference.add_user_message("Hi")

        with pytest.raises(PromptTooLongError):
            await inference.complete()

    @pytest.mark.asyncio
    async def test_reraises_other_bad_request(self):
        from openai import BadRequestError

        client = _mock_client()
        client.responses.create = AsyncMock(
            side_effect=BadRequestError(
                message="invalid API version",
                response=MagicMock(status_code=400),
                body={"error": {"message": "invalid"}},
            )
        )

        inference = OpenAIResponseInference(client, model_name="test-model")
        inference.add_user_message("Hi")

        with pytest.raises(BadRequestError):
            await inference.complete()


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


def _make_stream_events() -> list:
    """Build mock streaming events for a function call response."""
    # Event 1: output item added (function_call)
    fc_item = _make_obj(type="function_call", call_id="call_1", name="list_dirs", arguments="")
    ev1 = _make_obj(type="response.output_item.added", item=fc_item, output_index=0)

    # Event 2: function call arguments delta
    ev2 = _make_obj(type="response.function_call_arguments.delta", output_index=0, delta='{"path":')

    # Event 3: function call arguments delta
    ev3 = _make_obj(type="response.function_call_arguments.delta", output_index=0, delta='"/tmp"}')

    # Event 4: function call arguments done
    ev4 = _make_obj(type="response.function_call_arguments.done", output_index=0, arguments='{"path":"/tmp"}')

    # Event 5: completed with usage
    fc_done = _make_obj(
        type="function_call",
        call_id="call_1",
        name="list_dirs",
        arguments='{"path":"/tmp"}',
        model_dump=lambda: {
            "type": "function_call",
            "call_id": "call_1",
            "name": "list_dirs",
            "arguments": '{"path":"/tmp"}',
        },
    )
    completed_response = _make_obj(
        output=[fc_done],
        usage=_make_obj(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            input_tokens_details=_make_obj(cached_tokens=2),
        ),
        model="gpt-4.1",
        id="resp_stream",
    )
    ev5 = _make_obj(type="response.completed", response=completed_response)

    return [ev1, ev2, ev3, ev4, ev5]


def _make_text_stream_events() -> list:
    """Build mock streaming events for a text response."""
    ev1 = _make_obj(type="response.output_text.delta", delta="Hello, ")
    ev2 = _make_obj(type="response.output_text.delta", delta="world!")

    text_block = _make_obj(type="output_text", text="Hello, world!")
    message_item = _make_obj(
        type="message",
        content=[text_block],
        model_dump=lambda: {"type": "message", "content": [{"type": "output_text", "text": "Hello, world!"}]},
    )
    completed_response = _make_obj(
        output=[message_item],
        usage=_make_obj(
            input_tokens=8,
            output_tokens=3,
            total_tokens=11,
            input_tokens_details=_make_obj(cached_tokens=0),
        ),
        model="gpt-4.1",
        id="resp_text",
    )
    ev3 = _make_obj(type="response.completed", response=completed_response)
    return [ev1, ev2, ev3]


@pytest.mark.asyncio
async def test_stream_assembles_tool_calls():
    """Streaming assembles function_call events into ToolCall objects."""
    events = _make_stream_events()

    async def _fake_stream():
        for e in events:
            yield e

    client = _mock_client()
    client.responses.create.return_value = _fake_stream()

    inference = OpenAIResponseInference(client, model_name="test-model")
    inference.add_user_message("List dirs")

    collected: list = []
    async for event in inference.stream():
        collected.append(event)

    # Last event has assembled tool calls
    final = collected[-1]
    assert final.tool_calls
    tc = final.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.name == "list_dirs"
    assert tc.arguments == '{"path":"/tmp"}'
    assert tc.id == "call_1"

    # Usage populated from completed event
    assert final.usage is not None
    assert final.usage.input_tokens == 10
    assert final.usage.output_tokens == 5
    assert final.usage.cached_input_tokens == 2


@pytest.mark.asyncio
async def test_stream_text_response():
    """Streaming yields text deltas and assembles final content."""
    events = _make_text_stream_events()

    async def _fake_stream():
        for e in events:
            yield e

    client = _mock_client()
    client.responses.create.return_value = _fake_stream()

    inference = OpenAIResponseInference(client, model_name="test-model")
    inference.add_user_message("Hello")

    collected: list = []
    async for event in inference.stream():
        collected.append(event)

    # Should have text deltas followed by final event
    text_deltas = [e for e in collected if e.delta and e.finish_reason is None]
    assert len(text_deltas) == 2
    assert text_deltas[0].delta == "Hello, "
    assert text_deltas[1].delta == "world!"

    # Final event
    final = collected[-1]
    assert final.tool_calls == []
    assert final.finish_reason == "stop"
    assert final.usage is not None
    assert final.usage.input_tokens == 8


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------


class TestConversationState:
    """Tests for conversation management methods."""

    def test_add_user_message(self):
        inference = OpenAIResponseInference(_mock_client(), model_name="test-model")
        inference.add_user_message("Hello")
        assert inference._messages == [{"role": "user", "content": "Hello"}]

    def test_add_tool_results(self):
        inference = OpenAIResponseInference(_mock_client(), model_name="test-model")
        inference.add_tool_results(
            [
                ToolResult(tool_call_id="call_1", content="result1"),
                ToolResult(tool_call_id="call_2", content="result2"),
            ]
        )
        assert len(inference._messages) == 2
        assert inference._messages[0]["type"] == "function_call_output"
        assert inference._messages[0]["call_id"] == "call_1"
        assert inference._messages[1]["output"] == "result2"

    def test_reset(self):
        inference = OpenAIResponseInference(_mock_client(), model_name="test-model")
        inference.add_user_message("Hello")
        inference.reset()
        assert inference._messages == []

    def test_reset_preserves_instructions(self):
        inference = OpenAIResponseInference(_mock_client(), model_name="test-model")
        inference.set_system_prompt("Be helpful.")
        inference.add_user_message("Hi")
        inference.reset()
        assert inference._instructions == "Be helpful."
        assert inference._messages == []

    def test_replace_history(self):
        inference = OpenAIResponseInference(_mock_client(), model_name="test-model")
        inference.add_user_message("msg1")
        inference.add_user_message("msg2")
        inference.replace_history("Summary of conversation")
        assert len(inference._messages) == 1
        assert "Summary of conversation" in inference._messages[0]["content"]

    def test_get_messages(self):
        inference = OpenAIResponseInference(_mock_client(), model_name="test-model")
        inference.set_system_prompt("System prompt")
        inference.add_user_message("Hello")
        msgs = inference.get_messages()
        assert len(msgs) == 2
        assert msgs[0] == {"role": "system", "content": "System prompt"}
        assert msgs[1] == {"role": "user", "content": "Hello"}
