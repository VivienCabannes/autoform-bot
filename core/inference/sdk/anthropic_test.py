# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for AnthropicInference — content block parsing, tool calls, and streaming."""

from __future__ import annotations

import json
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.inference import InferenceConfig, ToolCall, ToolResult, ToolSchema
from ..sdk.anthropic import AnthropicInference, _map_stop_reason, _schemas_to_anthropic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_obj(**kwargs: Any) -> types.SimpleNamespace:
    """Create a SimpleNamespace (attribute-style object) from kwargs."""
    return types.SimpleNamespace(**kwargs)


def _make_inference(client: Any | None = None) -> AnthropicInference:
    """Create an AnthropicInference with a mock client."""
    if client is None:
        client = AsyncMock()
    return AnthropicInference(client, model_name="test-model")


# ---------------------------------------------------------------------------
# _schemas_to_anthropic
# ---------------------------------------------------------------------------


class TestSchemasToAnthropic:
    def test_converts_tool_schemas(self):
        schemas = [
            ToolSchema(name="read_file", description="Read a file", parameters={"type": "object", "properties": {}}),
            ToolSchema(name="write_file", description="Write a file", parameters={"type": "object"}),
        ]
        result = _schemas_to_anthropic(schemas)
        assert len(result) == 2
        assert result[0] == {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}},
        }
        assert result[1]["name"] == "write_file"
        assert "input_schema" in result[1]

    def test_empty_list(self):
        assert _schemas_to_anthropic([]) == []


# ---------------------------------------------------------------------------
# _map_stop_reason
# ---------------------------------------------------------------------------


class TestMapStopReason:
    def test_end_turn(self):
        assert _map_stop_reason("end_turn") == "stop"

    def test_tool_use(self):
        assert _map_stop_reason("tool_use") == "tool_calls"

    def test_max_tokens(self):
        assert _map_stop_reason("max_tokens") == "length"

    def test_none(self):
        assert _map_stop_reason(None) == "stop"

    def test_unknown(self):
        assert _map_stop_reason("something_else") == "something_else"


# ---------------------------------------------------------------------------
# Conversation management
# ---------------------------------------------------------------------------


class TestConversationManagement:
    def test_add_user_message(self):
        inf = _make_inference()
        inf.add_user_message("hello")
        assert inf._messages == [{"role": "user", "content": "hello"}]

    def test_add_tool_results(self):
        inf = _make_inference()
        results = [
            ToolResult(tool_call_id="tc_1", content="result1", is_error=False),
            ToolResult(tool_call_id="tc_2", content="error!", is_error=True),
        ]
        inf.add_tool_results(results)
        assert len(inf._messages) == 1
        msg = inf._messages[0]
        assert msg["role"] == "user"
        assert len(msg["content"]) == 2
        assert msg["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "tc_1",
            "content": "result1",
            "is_error": False,
        }
        assert msg["content"][1]["is_error"] is True

    def test_set_system_prompt(self):
        inf = _make_inference()
        inf.set_system_prompt("You are helpful.")
        assert inf._system_prompt == "You are helpful."

    def test_reset(self):
        inf = _make_inference()
        inf.add_user_message("hello")
        inf.reset()
        assert inf._messages == []

    def test_get_messages_includes_system(self):
        inf = _make_inference()
        inf.set_system_prompt("system")
        inf.add_user_message("hello")
        msgs = inf.get_messages()
        assert msgs[0] == {"role": "system", "content": "system"}
        assert msgs[1] == {"role": "user", "content": "hello"}

    def test_get_messages_no_system(self):
        inf = _make_inference()
        inf.add_user_message("hello")
        msgs = inf.get_messages()
        assert len(msgs) == 1

    def test_replace_history(self):
        inf = _make_inference()
        inf.add_user_message("msg1")
        inf.add_user_message("msg2")
        inf.replace_history("summary")
        assert len(inf._messages) == 1
        assert "summary" in inf._messages[0]["content"]


# ---------------------------------------------------------------------------
# _append_assistant_message
# ---------------------------------------------------------------------------


class TestAppendAssistantMessage:
    def test_text_only(self):
        inf = _make_inference()
        inf._append_assistant_message("hello", "", [])
        msg = inf._messages[0]
        assert msg["role"] == "assistant"
        assert msg["content"] == [{"type": "text", "text": "hello"}]

    def test_thinking_and_text(self):
        inf = _make_inference()
        inf._append_assistant_message("answer", "reasoning", [])
        content = inf._messages[0]["content"]
        assert content[0] == {"type": "thinking", "thinking": "reasoning"}
        assert content[1] == {"type": "text", "text": "answer"}

    def test_with_tool_calls(self):
        inf = _make_inference()
        tc = ToolCall(id="tc_1", name="read_file", arguments='{"path": "/tmp"}')
        inf._append_assistant_message("", "", [tc])
        content = inf._messages[0]["content"]
        assert content[0]["type"] == "tool_use"
        assert content[0]["id"] == "tc_1"
        assert content[0]["name"] == "read_file"
        assert content[0]["input"] == {"path": "/tmp"}

    def test_empty_falls_back_to_string(self):
        inf = _make_inference()
        inf._append_assistant_message("", "", [])
        msg = inf._messages[0]
        assert msg["content"] == ""


# ---------------------------------------------------------------------------
# _parse_usage
# ---------------------------------------------------------------------------


class TestParseUsage:
    def test_standard_usage(self):
        inf = _make_inference()
        usage_obj = _make_obj(input_tokens=100, output_tokens=50)
        usage = inf._parse_usage(usage_obj)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.total_tokens == 150
        assert usage.cached_input_tokens == 0

    def test_cached_tokens(self):
        inf = _make_inference()
        usage_obj = _make_obj(input_tokens=100, output_tokens=50, cache_read_input_tokens=30)
        usage = inf._parse_usage(usage_obj)
        assert usage.cached_input_tokens == 30
        assert usage.input_tokens == 130  # 100 uncached + 30 cache_read

    def test_missing_fields(self):
        inf = _make_inference()
        usage_obj = _make_obj()
        usage = inf._parse_usage(usage_obj)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_text_response():
    """complete() parses a text-only response."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[_make_obj(type="text", text="Hello world")],
        usage=_make_obj(input_tokens=10, output_tokens=5),
        model="claude-sonnet-4-20250514",
        id="msg_123",
        stop_reason="end_turn",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("Hi")
    result = await inf.complete()

    assert result.text == "Hello world"
    assert result.thinking == ""
    assert result.tool_calls == []
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.model == "claude-sonnet-4-20250514"
    assert result.call_id == "msg_123"


@pytest.mark.asyncio
async def test_complete_with_thinking():
    """complete() parses thinking blocks."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[
            _make_obj(type="thinking", thinking="Let me think..."),
            _make_obj(type="text", text="The answer is 42"),
        ],
        usage=_make_obj(input_tokens=20, output_tokens=10),
        model="claude-sonnet-4-20250514",
        id="msg_456",
        stop_reason="end_turn",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("What is the meaning of life?")
    result = await inf.complete()

    assert result.thinking == "Let me think..."
    assert result.text == "The answer is 42"


@pytest.mark.asyncio
async def test_complete_with_tool_use():
    """complete() parses tool_use blocks into ToolCall objects."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[
            _make_obj(type="text", text="I'll read that file."),
            _make_obj(type="tool_use", id="tu_1", name="read_file", input={"path": "/tmp/test.txt"}),
        ],
        usage=_make_obj(input_tokens=15, output_tokens=8),
        model="claude-sonnet-4-20250514",
        id="msg_789",
        stop_reason="tool_use",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("Read /tmp/test.txt")
    result = await inf.complete()

    assert result.text == "I'll read that file."
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "tu_1"
    assert tc.name == "read_file"
    assert json.loads(tc.arguments) == {"path": "/tmp/test.txt"}


@pytest.mark.asyncio
async def test_complete_appends_to_history():
    """complete() auto-appends the assistant message to internal history."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[_make_obj(type="text", text="response")],
        usage=_make_obj(input_tokens=5, output_tokens=3),
        model="test",
        id="msg_1",
        stop_reason="end_turn",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("hello")
    await inf.complete()

    assert len(inf._messages) == 2  # user + assistant
    assert inf._messages[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# Cache breakpoints
# ---------------------------------------------------------------------------


class TestApplyCacheBreakpoints:
    def test_no_cache_config_leaves_kwargs_unchanged(self):
        inf = _make_inference()
        inf.set_system_prompt("You are helpful.")
        inf.add_user_message("hi")
        kwargs = inf._build_kwargs(InferenceConfig(), [ToolSchema("t", "d", {})])
        assert kwargs["system"] == "You are helpful."
        assert "cache_control" not in kwargs["tools"][0]

    def test_system_string_to_block_list(self):
        from core.inference import CacheConfig

        inf = _make_inference()
        inf.set_system_prompt("You are helpful.")
        inf.add_user_message("hi")
        config = InferenceConfig(cache=CacheConfig(system=True))
        kwargs = inf._build_kwargs(config, None)
        assert isinstance(kwargs["system"], list)
        assert len(kwargs["system"]) == 1
        assert kwargs["system"][0]["type"] == "text"
        assert kwargs["system"][0]["text"] == "You are helpful."
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_system_no_prompt_no_error(self):
        from core.inference import CacheConfig

        inf = _make_inference()
        inf.add_user_message("hi")
        config = InferenceConfig(cache=CacheConfig(system=True))
        kwargs = inf._build_kwargs(config, None)
        assert "system" not in kwargs

    def test_tools_breakpoint(self):
        from core.inference import CacheConfig

        inf = _make_inference()
        inf.add_user_message("hi")
        tools = [
            ToolSchema("tool_a", "desc a", {"type": "object"}),
            ToolSchema("tool_b", "desc b", {"type": "object"}),
        ]
        config = InferenceConfig(cache=CacheConfig(system=False, tools=True))
        kwargs = inf._build_kwargs(config, tools)
        assert "cache_control" not in kwargs["tools"][0]
        assert kwargs["tools"][1]["cache_control"] == {"type": "ephemeral"}

    def test_tools_breakpoint_no_tools(self):
        from core.inference import CacheConfig

        inf = _make_inference()
        inf.add_user_message("hi")
        config = InferenceConfig(cache=CacheConfig(system=False, tools=True))
        kwargs = inf._build_kwargs(config, None)
        assert "tools" not in kwargs

    def test_messages_breakpoint_string_content(self):
        from core.inference import CacheConfig

        inf = _make_inference()
        inf.add_user_message("hello world")
        config = InferenceConfig(cache=CacheConfig(system=False, messages=True))
        kwargs = inf._build_kwargs(config, None)
        last_msg = kwargs["messages"][-1]
        assert isinstance(last_msg["content"], list)
        assert last_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_messages_breakpoint_list_content(self):
        from core.inference import CacheConfig

        inf = _make_inference()
        inf.add_tool_results([ToolResult(tool_call_id="tc_1", content="result")])
        config = InferenceConfig(cache=CacheConfig(system=False, messages=True))
        kwargs = inf._build_kwargs(config, None)
        last_msg = kwargs["messages"][-1]
        assert isinstance(last_msg["content"], list)
        assert last_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_messages_breakpoint_does_not_mutate_internal_state(self):
        from core.inference import CacheConfig

        inf = _make_inference()
        inf.add_user_message("hello")
        config = InferenceConfig(cache=CacheConfig(system=False, messages=True))
        inf._build_kwargs(config, None)
        assert inf._messages[0]["content"] == "hello"

    def test_all_breakpoints(self):
        from core.inference import CacheConfig

        inf = _make_inference()
        inf.set_system_prompt("system prompt")
        inf.add_user_message("hello")
        tools = [ToolSchema("t", "d", {"type": "object"})]
        config = InferenceConfig(cache=CacheConfig(system=True, tools=True, messages=True))
        kwargs = inf._build_kwargs(config, tools)
        assert isinstance(kwargs["system"], list)
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert kwargs["tools"][0]["cache_control"] == {"type": "ephemeral"}
        last_msg = kwargs["messages"][-1]
        assert isinstance(last_msg["content"], list)
        assert last_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# _parse_usage with cache creation tokens
# ---------------------------------------------------------------------------


class TestParseUsageWithCaching:
    def test_cache_creation_tokens(self):
        inf = _make_inference()
        usage_obj = _make_obj(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=100,
        )
        usage = inf._parse_usage(usage_obj)
        assert usage.input_tokens == 110  # 10 + 0 + 100
        assert usage.cache_creation_input_tokens == 100
        assert usage.cached_input_tokens == 0
        assert usage.total_tokens == 115

    def test_cache_read_tokens(self):
        inf = _make_inference()
        usage_obj = _make_obj(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=0,
        )
        usage = inf._parse_usage(usage_obj)
        assert usage.input_tokens == 110  # 10 + 100 + 0
        assert usage.cached_input_tokens == 100
        assert usage.cache_creation_input_tokens == 0

    def test_both_cache_fields(self):
        inf = _make_inference()
        usage_obj = _make_obj(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=50,
            cache_creation_input_tokens=40,
        )
        usage = inf._parse_usage(usage_obj)
        assert usage.input_tokens == 100  # 10 + 50 + 40
        assert usage.cached_input_tokens == 50
        assert usage.cache_creation_input_tokens == 40
        assert usage.total_tokens == 105


@pytest.mark.asyncio
async def test_complete_passes_system_prompt():
    """complete() passes system prompt as a top-level kwarg."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[_make_obj(type="text", text="ok")],
        usage=_make_obj(input_tokens=5, output_tokens=1),
        model="test",
        id="msg_1",
        stop_reason="end_turn",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.set_system_prompt("Be helpful.")
    inf.add_user_message("hi")
    await inf.complete()

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["system"] == "Be helpful."
    # System prompt should NOT appear in messages list
    for msg in call_kwargs["messages"]:
        assert msg["role"] != "system"


@pytest.mark.asyncio
async def test_complete_uses_inference_config_default_max_tokens():
    """complete() uses InferenceConfig.max_tokens default (16384) over backend default."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[_make_obj(type="text", text="ok")],
        usage=_make_obj(input_tokens=5, output_tokens=1),
        model="test",
        id="msg_1",
        stop_reason="end_turn",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model", default_max_tokens=8192)
    inf.add_user_message("hi")
    await inf.complete()

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["max_tokens"] == 16384  # InferenceConfig default takes precedence


@pytest.mark.asyncio
async def test_complete_uses_config_max_tokens():
    """complete() prefers InferenceConfig.max_tokens over default."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[_make_obj(type="text", text="ok")],
        usage=_make_obj(input_tokens=5, output_tokens=1),
        model="test",
        id="msg_1",
        stop_reason="end_turn",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model", default_max_tokens=8192)
    inf.add_user_message("hi")
    await inf.complete(inference_config=InferenceConfig(max_tokens=2048))

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["max_tokens"] == 2048


# ---------------------------------------------------------------------------
# finish_reason propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_finish_reason_end_turn():
    """complete() maps Anthropic 'end_turn' to finish_reason='stop'."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[_make_obj(type="text", text="Done")],
        usage=_make_obj(input_tokens=5, output_tokens=1),
        model="test",
        id="msg_1",
        stop_reason="end_turn",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("hi")
    result = await inf.complete()

    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_complete_finish_reason_max_tokens():
    """complete() maps Anthropic 'max_tokens' to finish_reason='length'."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[_make_obj(type="text", text="truncated output")],
        usage=_make_obj(input_tokens=5, output_tokens=100),
        model="test",
        id="msg_2",
        stop_reason="max_tokens",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("write a long essay")
    result = await inf.complete()

    assert result.finish_reason == "length"


@pytest.mark.asyncio
async def test_complete_finish_reason_tool_use():
    """complete() maps Anthropic 'tool_use' to finish_reason='tool_calls'."""
    mock_client = AsyncMock()
    response = _make_obj(
        content=[
            _make_obj(type="tool_use", id="tu_1", name="read_file", input={"path": "/tmp"}),
        ],
        usage=_make_obj(input_tokens=5, output_tokens=10),
        model="test",
        id="msg_3",
        stop_reason="tool_use",
    )
    mock_client.messages.create = AsyncMock(return_value=response)

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("read /tmp")
    result = await inf.complete()

    assert result.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# PromptTooLongError detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_raises_prompt_too_long():
    """complete() raises PromptTooLongError on prompt-too-long BadRequestError."""
    from anthropic import BadRequestError as AnthropicBadRequest

    from core.inference import PromptTooLongError

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(
        side_effect=AnthropicBadRequest(
            message="prompt is too long: 300000 tokens > 200000 maximum",
            response=MagicMock(status_code=400),
            body={"error": {"message": "prompt is too long"}},
        )
    )

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("hi")

    with pytest.raises(PromptTooLongError):
        await inf.complete()


@pytest.mark.asyncio
async def test_complete_reraises_other_bad_request():
    """complete() re-raises non-prompt-too-long BadRequestError."""
    from anthropic import BadRequestError as AnthropicBadRequest

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(
        side_effect=AnthropicBadRequest(
            message="invalid model name",
            response=MagicMock(status_code=400),
            body={"error": {"message": "invalid model name"}},
        )
    )

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("hi")

    with pytest.raises(AnthropicBadRequest):
        await inf.complete()


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


class _FakeStreamContext:
    """Mock for the Anthropic stream context manager."""

    def __init__(self, events: list, final_message: Any = None):
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def __aiter__(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return self._final_message


@pytest.mark.asyncio
async def test_stream_text_deltas():
    """stream() yields text deltas from content_block_delta events."""
    events = [
        _make_obj(type="content_block_delta", delta=_make_obj(type="text_delta", text="Hello ")),
        _make_obj(type="content_block_delta", delta=_make_obj(type="text_delta", text="world")),
        _make_obj(
            type="message_delta",
            delta=_make_obj(stop_reason="end_turn"),
            usage=_make_obj(input_tokens=10, output_tokens=5),
        ),
    ]
    final = _make_obj(usage=_make_obj(input_tokens=10, output_tokens=5))

    mock_client = AsyncMock()
    mock_client.messages.stream = MagicMock(return_value=_FakeStreamContext(events, final))

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("Hi")

    stream_events = []
    async for event in inf.stream():
        stream_events.append(event)

    # Two text deltas + final event
    assert len(stream_events) == 3
    assert stream_events[0].delta == "Hello "
    assert stream_events[1].delta == "world"
    final_event = stream_events[-1]
    assert final_event.finish_reason == "stop"
    assert final_event.usage is not None
    assert final_event.usage.input_tokens == 10


@pytest.mark.asyncio
async def test_stream_thinking_deltas():
    """stream() yields thinking deltas."""
    events = [
        _make_obj(type="content_block_delta", delta=_make_obj(type="thinking_delta", thinking="Let me ")),
        _make_obj(type="content_block_delta", delta=_make_obj(type="thinking_delta", thinking="think...")),
        _make_obj(type="content_block_delta", delta=_make_obj(type="text_delta", text="42")),
        _make_obj(
            type="message_delta",
            delta=_make_obj(stop_reason="end_turn"),
            usage=_make_obj(input_tokens=10, output_tokens=5),
        ),
    ]
    final = _make_obj(usage=_make_obj(input_tokens=10, output_tokens=5))

    mock_client = AsyncMock()
    mock_client.messages.stream = MagicMock(return_value=_FakeStreamContext(events, final))

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("Think")

    stream_events = []
    async for event in inf.stream():
        stream_events.append(event)

    assert stream_events[0].thinking == "Let me "
    assert stream_events[1].thinking == "think..."
    assert stream_events[2].delta == "42"


@pytest.mark.asyncio
async def test_stream_tool_calls():
    """stream() assembles tool calls from content_block_start + input_json_delta events."""
    events = [
        _make_obj(
            type="content_block_start",
            index=0,
            content_block=_make_obj(type="tool_use", id="tu_1", name="read_file"),
        ),
        _make_obj(type="content_block_delta", delta=_make_obj(type="input_json_delta", partial_json='{"path":')),
        _make_obj(type="content_block_delta", delta=_make_obj(type="input_json_delta", partial_json='"/tmp"}')),
        _make_obj(type="content_block_stop"),
        _make_obj(
            type="message_delta",
            delta=_make_obj(stop_reason="tool_use"),
            usage=_make_obj(input_tokens=10, output_tokens=5),
        ),
    ]
    final = _make_obj(usage=_make_obj(input_tokens=10, output_tokens=5))

    mock_client = AsyncMock()
    mock_client.messages.stream = MagicMock(return_value=_FakeStreamContext(events, final))

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("Read file")

    stream_events = []
    async for event in inf.stream():
        stream_events.append(event)

    final_event = stream_events[-1]
    assert final_event.finish_reason == "tool_calls"
    assert len(final_event.tool_calls) == 1
    tc = final_event.tool_calls[0]
    assert tc.id == "tu_1"
    assert tc.name == "read_file"
    assert json.loads(tc.arguments) == {"path": "/tmp"}


@pytest.mark.asyncio
async def test_stream_appends_to_history():
    """stream() auto-appends the assistant message to internal history."""
    events = [
        _make_obj(type="content_block_delta", delta=_make_obj(type="text_delta", text="hi")),
        _make_obj(
            type="message_delta",
            delta=_make_obj(stop_reason="end_turn"),
            usage=_make_obj(input_tokens=5, output_tokens=1),
        ),
    ]
    final = _make_obj(usage=_make_obj(input_tokens=5, output_tokens=1))

    mock_client = AsyncMock()
    mock_client.messages.stream = MagicMock(return_value=_FakeStreamContext(events, final))

    inf = AnthropicInference(mock_client, model_name="test-model")
    inf.add_user_message("hello")

    async for _ in inf.stream():
        pass

    assert len(inf._messages) == 2  # user + assistant
    assert inf._messages[1]["role"] == "assistant"
