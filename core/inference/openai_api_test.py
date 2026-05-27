# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for OpenAIInference — tool call normalization and streaming."""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.inference import ToolCall
from .openai_api import (
    OpenAIInference,
    _build_tool_call,
    sanitize_accumulated_tool_calls,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_obj(**kwargs: Any) -> types.SimpleNamespace:
    """Create a SimpleNamespace (attribute-style object) from kwargs."""
    return types.SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# _normalize_tool_calls
# ---------------------------------------------------------------------------


class TestNormalizeToolCalls:
    """Tests for the _normalize_tool_calls class method."""

    def test_normalizes_dict_to_tool_call(self):
        raw = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_dirs", "arguments": "{}"},
            }
        ]
        result = OpenAIInference._normalize_tool_calls(raw)
        assert len(result) == 1
        assert isinstance(result[0], ToolCall)
        assert result[0].id == "call_1"
        assert result[0].name == "list_dirs"
        assert result[0].arguments == "{}"

    def test_normalizes_object_to_tool_call(self):
        fn = _make_obj(name="list_dirs", arguments="{}")
        call = _make_obj(
            id="call_1",
            type="function",
            function=fn,
        )
        result = OpenAIInference._normalize_tool_calls([call])
        assert len(result) == 1
        assert isinstance(result[0], ToolCall)
        assert result[0].id == "call_1"
        assert result[0].name == "list_dirs"

    def test_keeps_empty_name(self):
        raw = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "", "arguments": "{}"},
            }
        ]
        result = OpenAIInference._normalize_tool_calls(raw)
        assert len(result) == 1
        assert result[0].name == ""

    def test_assigns_default_id(self):
        raw = [
            {
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ]
        result = OpenAIInference._normalize_tool_calls(raw)
        assert result[0].id == "call_0"

    def test_multiple_tool_calls(self):
        raw = [
            {"id": "a", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "f2", "arguments": '{"x":1}'}},
        ]
        result = OpenAIInference._normalize_tool_calls(raw)
        assert len(result) == 2
        assert result[0].name == "f1"
        assert result[1].name == "f2"
        assert result[1].arguments == '{"x":1}'


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------


def _make_stream_chunks() -> list:
    """Build mock SSE chunks with tool call deltas."""
    # Chunk 1: tool call start
    tc_delta = _make_obj(
        index=0,
        id="call_1",
        type="function",
        function=_make_obj(name="list_dirs", arguments=""),
    )
    delta1 = _make_obj(content=None, tool_calls=[tc_delta])
    choice1 = _make_obj(delta=delta1, finish_reason=None)
    chunk1 = _make_obj(choices=[choice1], usage=None)

    # Chunk 2: argument fragment
    tc_delta2 = _make_obj(
        index=0,
        id=None,
        type=None,
        function=_make_obj(name=None, arguments='{"path":'),
    )
    delta2 = _make_obj(content=None, tool_calls=[tc_delta2])
    choice2 = _make_obj(delta=delta2, finish_reason=None)
    chunk2 = _make_obj(choices=[choice2], usage=None)

    # Chunk 3: argument fragment + finish
    tc_delta3 = _make_obj(
        index=0,
        id=None,
        type=None,
        function=_make_obj(name=None, arguments='"/tmp"}'),
    )
    delta3 = _make_obj(content=None, tool_calls=[tc_delta3])
    choice3 = _make_obj(delta=delta3, finish_reason="tool_calls")
    chunk3 = _make_obj(choices=[choice3], usage=None)

    # Chunk 4: usage-only
    chunk4 = _make_obj(
        choices=[],
        usage=_make_obj(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )

    return [chunk1, chunk2, chunk3, chunk4]


@pytest.mark.asyncio
async def test_stream_assembles_tool_calls():
    """Streaming assembles tool call deltas into ToolCall objects."""
    chunks = _make_stream_chunks()

    async def _fake_stream():
        for c in chunks:
            yield c

    mock_client = AsyncMock(spec=["chat"])
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_fake_stream())

    inference = OpenAIInference(mock_client, model_name="test-model")

    events: list = []
    async for event in inference.stream():
        events.append(event)

    # Last event has the assembled tool calls
    final = events[-1]
    assert final.tool_calls
    tc = final.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.name == "list_dirs"
    assert tc.arguments == '{"path":"/tmp"}'
    assert tc.id == "call_1"

    # Usage should be populated
    assert final.usage is not None
    assert final.usage.input_tokens == 10
    assert final.usage.output_tokens == 5


# ---------------------------------------------------------------------------
# complete() finish_reason propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_finish_reason_stop():
    """complete() propagates finish_reason='stop' from the API response."""
    mock_client = AsyncMock(spec=["chat"])
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()

    msg = _make_obj(content="Hello", tool_calls=None)
    choice = _make_obj(message=msg, finish_reason="stop")
    usage = _make_obj(
        prompt_tokens=5,
        completion_tokens=2,
        total_tokens=7,
        prompt_tokens_details=None,
    )
    response = _make_obj(choices=[choice], usage=usage, model="gpt-4.1", id="resp_1")
    mock_client.chat.completions.create = AsyncMock(return_value=response)

    inference = OpenAIInference(mock_client, model_name="gpt-4.1")
    inference.add_user_message("Hi")
    result = await inference.complete()

    assert result.finish_reason == "stop"
    assert result.text == "Hello"


@pytest.mark.asyncio
async def test_complete_finish_reason_length():
    """complete() propagates finish_reason='length' when output is truncated."""
    mock_client = AsyncMock(spec=["chat"])
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()

    msg = _make_obj(content="truncated output", tool_calls=None)
    choice = _make_obj(message=msg, finish_reason="length")
    usage = _make_obj(
        prompt_tokens=5,
        completion_tokens=100,
        total_tokens=105,
        prompt_tokens_details=None,
    )
    response = _make_obj(choices=[choice], usage=usage, model="gpt-4.1", id="resp_2")
    mock_client.chat.completions.create = AsyncMock(return_value=response)

    inference = OpenAIInference(mock_client, model_name="gpt-4.1")
    inference.add_user_message("Write a long essay")
    result = await inference.complete()

    assert result.finish_reason == "length"
    assert result.text == "truncated output"


# ---------------------------------------------------------------------------
# PromptTooLongError detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_raises_prompt_too_long():
    """complete() raises PromptTooLongError when BadRequestError indicates context overflow."""
    from openai import BadRequestError

    from core.inference import PromptTooLongError

    mock_client = AsyncMock(spec=["chat"])
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=BadRequestError(
            message="This model's maximum context length is 128000 tokens.",
            response=MagicMock(status_code=400),
            body={"error": {"message": "maximum context length"}},
        )
    )

    inference = OpenAIInference(mock_client, model_name="gpt-4.1")
    inference.add_user_message("Hi")

    with pytest.raises(PromptTooLongError):
        await inference.complete()


@pytest.mark.asyncio
async def test_complete_reraises_other_bad_request():
    """complete() re-raises non-prompt-too-long BadRequestError."""
    from openai import BadRequestError

    mock_client = AsyncMock(spec=["chat"])
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=BadRequestError(
            message="invalid API version",
            response=MagicMock(status_code=400),
            body={"error": {"message": "invalid API version"}},
        )
    )

    inference = OpenAIInference(mock_client, model_name="gpt-4.1")
    inference.add_user_message("Hi")

    with pytest.raises(BadRequestError):
        await inference.complete()


# ---------------------------------------------------------------------------
# _build_tool_call
# ---------------------------------------------------------------------------


class TestBuildToolCall:
    """Tests for _build_tool_call helper."""

    def test_valid_tool_call(self):
        result = _build_tool_call("read_file", '{"path":"/tmp"}', "call_1", 0)
        assert len(result) == 1
        assert result[0].name == "read_file"
        assert result[0].id == "call_1"
        assert result[0].truncated is False

    def test_empty_name_kept(self):
        result = _build_tool_call("", "{}", "call_1", 0)
        assert len(result) == 1
        assert result[0].name == ""

    def test_invalid_json_marks_truncated(self):
        result = _build_tool_call("f", "not json", "call_1", 0)
        assert len(result) == 1
        assert result[0].truncated is True
        assert result[0].arguments == "not json"  # original preserved

    def test_truncated_json_marks_truncated(self):
        truncated_args = '{"path": "plan.md", "content": "# Long content that got cut'
        result = _build_tool_call("scratchpad_write", truncated_args, "call_1", 0)
        assert len(result) == 1
        assert result[0].truncated is True
        assert result[0].arguments == truncated_args

    def test_fallback_id(self):
        result = _build_tool_call("f", "{}", "", 5)
        assert result[0].id == "call_5"


# ---------------------------------------------------------------------------
# sanitize_accumulated_tool_calls with recovery
# ---------------------------------------------------------------------------


class TestSanitize:
    """Test sanitize_accumulated_tool_calls."""

    def test_normal_tool_call(self):
        accumulated = {
            0: {"id": "call_1", "name": "list_dirs", "arguments": "{}"},
        }
        result = sanitize_accumulated_tool_calls(accumulated)
        assert len(result) == 1
        assert result[0].name == "list_dirs"
