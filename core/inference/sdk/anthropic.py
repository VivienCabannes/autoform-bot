# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Anthropic SDK inference backend.

Implements InferenceProtocol using the native AsyncAnthropic client.
Provides proper content block handling and native tool use format.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from anthropic import APITimeoutError, AsyncAnthropic, BadRequestError, RateLimitError

from ..protocol import (
    CacheConfig,
    DEFAULT_MAX_TOKENS,
    InferenceConfig,
    InferenceProtocol,
    PromptTooLongError,
    StreamEvent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolSchema,
    TurnResult,
    is_prompt_too_long_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format conversion helpers (internal)
# ---------------------------------------------------------------------------


def _schemas_to_anthropic(tools: list[ToolSchema]) -> list[dict[str, Any]]:
    """Convert ToolSchema list to Anthropic tool format."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]


def _map_stop_reason(reason: str | None) -> str:
    """Map Anthropic stop reasons to normalized finish reasons."""
    match reason:
        case "end_turn":
            return "stop"
        case "tool_use":
            return "tool_calls"
        case "max_tokens":
            return "length"
        case _:
            return reason or "stop"


def _apply_cache_breakpoints(kwargs: dict[str, Any], cache: CacheConfig) -> None:
    """Add ``cache_control`` markers to an Anthropic messages API request dict.

    Mutates *kwargs* in place.  Each enabled flag adds one explicit
    breakpoint (up to 4 allowed per request).
    """
    import copy

    # System prompt: convert string → content block list with cache_control.
    if cache.system and "system" in kwargs:
        system = kwargs["system"]
        if isinstance(system, str):
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(system, list) and system:
            # Already a content block list — annotate the last block.
            system[-1] = {**system[-1], "cache_control": {"type": "ephemeral"}}

    # Tools: annotate the last tool definition.
    if cache.tools and kwargs.get("tools"):
        tools = kwargs["tools"]
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}

    # Messages: annotate the last content block of the last message.
    if cache.messages and kwargs.get("messages"):
        messages = kwargs["messages"]
        last_msg = copy.deepcopy(messages[-1])
        content = last_msg["content"]
        if isinstance(content, str):
            last_msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list) and content:
            content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
        messages[-1] = last_msg


class AnthropicInference(InferenceProtocol):
    """InferenceProtocol implementation using AsyncAnthropic.

    Stateful: owns the conversation history as a list of Anthropic-format
    message dicts. The agent loop interacts via typed boundary methods.

    Args:
        client: AsyncAnthropic client.
        retries: Number of retries on transient errors.
        retry_delay: Seconds between retries.
        default_max_tokens: Fallback max_tokens (required by the Anthropic API).
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        model_name: str,
        retries: int = 3,
        retry_delay: float = 10.0,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        super().__init__()
        self._client = client
        self._model_name = model_name
        self._retries = retries
        self._retry_delay = retry_delay
        self._default_max_tokens = default_max_tokens

        # Conversation state
        self._system_prompt: str = ""
        self._messages: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Protocol: conversation management
    # ------------------------------------------------------------------

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    def get_system_prompt(self) -> str:
        return self._system_prompt

    def add_user_message(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.tool_call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in results
                ],
            }
        )

    def reset(self) -> None:
        self._messages.clear()

    def get_messages(self) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        if self._system_prompt:
            msgs.append({"role": "system", "content": self._system_prompt})
        msgs.extend(self._messages)
        return msgs

    def replace_history(self, summary: str) -> None:
        self._messages.clear()
        self._messages.append({"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"})

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [m for m in messages if m.get("role") != "system"]

    def cleanup_interrupted(self) -> None:
        while self._messages:
            last = self._messages[-1]
            role = last.get("role", "")
            content = last.get("content", "")
            if (
                role == "user"
                and isinstance(content, list)
                and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            ):
                self._messages.pop()
                continue
            if role == "assistant":
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in content
                ):
                    self._messages.pop()
                    continue
                if not content or (isinstance(content, str) and not content.strip()):
                    self._messages.pop()
                    continue
            break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_assistant_message(self, text: str, thinking: str, tool_calls: list[ToolCall]) -> None:
        """Append an assistant message to internal history after a response."""
        content: list[dict[str, Any]] = []
        if thinking:
            content.append({"type": "thinking", "thinking": thinking})
        if text:
            content.append({"type": "text", "text": text})
        for tc in tool_calls:
            try:
                tool_input = json.loads(tc.arguments) if tc.arguments else {}
            except json.JSONDecodeError:
                logger.warning(
                    "Tool call %r has invalid JSON arguments (truncated?), replacing with empty",
                    tc.name,
                )
                tool_input = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tool_input,
                }
            )
        self._messages.append({"role": "assistant", "content": content or text})

    def _build_kwargs(
        self,
        config: InferenceConfig,
        tools: list[ToolSchema] | None,
    ) -> dict[str, Any]:
        """Build keyword arguments for the Anthropic messages API."""
        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": list(self._messages),
            "max_tokens": config.max_tokens or self._default_max_tokens,
        }
        if self._system_prompt:
            kwargs["system"] = self._system_prompt
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.top_p is not None:
            kwargs["top_p"] = config.top_p
        if tools:
            kwargs["tools"] = _schemas_to_anthropic(tools)
        if config.cache:
            _apply_cache_breakpoints(kwargs, config.cache)
        return kwargs

    def _parse_usage(self, usage: Any) -> TokenUsage:
        """Extract TokenUsage from an Anthropic usage object."""
        uncached_input = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        total_input = uncached_input + cache_read + cache_creation
        return TokenUsage(
            input_tokens=total_input,
            output_tokens=output_tokens,
            cached_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            total_tokens=total_input + output_tokens,
        )

    # ------------------------------------------------------------------
    # Protocol: complete
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        return self._model_name

    async def complete(
        self,
        *,
        tools: list[ToolSchema] | None = None,
        inference_config: InferenceConfig | None = None,
    ) -> TurnResult:
        config = inference_config or InferenceConfig()
        kwargs = self._build_kwargs(config, tools)

        attempt = 0
        while True:
            try:
                response = await self._client.messages.create(**kwargs)
                break
            except BadRequestError as err:
                if is_prompt_too_long_message(str(err)):
                    raise PromptTooLongError(str(err)) from err
                raise
            except (APITimeoutError, RateLimitError, httpx.ReadTimeout, httpx.ConnectTimeout) as err:
                if attempt >= self._retries:
                    logger.warning("Call messages.create timed out; giving up: %s", err)
                    raise
                logger.warning(
                    "Call messages.create timed out; retrying %d/%d: %s",
                    attempt + 1,
                    self._retries,
                    err,
                )
                attempt += 1
                await asyncio.sleep(self._retry_delay)

        # Parse content blocks
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            match block.type:
                case "text":
                    text_parts.append(block.text)
                case "thinking":
                    thinking_parts.append(block.thinking)
                case "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=block.id,
                            name=block.name,
                            arguments=json.dumps(block.input),
                        )
                    )

        text = "".join(text_parts)
        thinking = "".join(thinking_parts)
        usage = self._parse_usage(response.usage)

        self._append_assistant_message(text, thinking, tool_calls)

        return TurnResult(
            text=text,
            thinking=thinking,
            tool_calls=tool_calls,
            usage=usage,
            model=response.model,
            call_id=response.id,
            finish_reason=_map_stop_reason(response.stop_reason),
        )

    # ------------------------------------------------------------------
    # Protocol: stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        *,
        tools: list[ToolSchema] | None = None,
        inference_config: InferenceConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        config = inference_config or InferenceConfig()
        kwargs = self._build_kwargs(config, tools)

        full_text = ""
        full_thinking = ""
        # {index: ToolCall} — accumulate tool calls by block index
        tool_call_map: dict[int, ToolCall] = {}
        # Track current tool call index for argument assembly
        current_tool_index: int | None = None
        usage = TokenUsage()
        finish_reason = "stop"

        # Retry covers both context-manager creation and __aenter__,
        # since messages.stream() defers the network call to __aenter__.
        attempt = 0
        while True:
            try:
                stream_ctx = self._client.messages.stream(**kwargs)
                stream = await stream_ctx.__aenter__()
                break
            except BadRequestError as err:
                if is_prompt_too_long_message(str(err)):
                    raise PromptTooLongError(str(err)) from err
                raise
            except (APITimeoutError, RateLimitError, httpx.ReadTimeout, httpx.ConnectTimeout) as err:
                if attempt >= self._retries:
                    raise
                logger.warning(
                    "stream connect failed; retrying %d/%d: %s",
                    attempt + 1,
                    self._retries,
                    err,
                )
                attempt += 1
                await asyncio.sleep(self._retry_delay)

        try:
            async for event in stream:
                match event.type:
                    case "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            idx = event.index
                            tool_call_map[idx] = ToolCall(
                                id=block.id,
                                name=block.name,
                                arguments="",
                            )
                            current_tool_index = idx

                    case "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            full_text += delta.text
                            yield StreamEvent(delta=delta.text)
                        elif delta.type == "thinking_delta":
                            full_thinking += delta.thinking
                            yield StreamEvent(thinking=delta.thinking)
                        elif delta.type == "input_json_delta":
                            if current_tool_index is not None and current_tool_index in tool_call_map:
                                tool_call_map[current_tool_index] = ToolCall(
                                    id=tool_call_map[current_tool_index].id,
                                    name=tool_call_map[current_tool_index].name,
                                    arguments=tool_call_map[current_tool_index].arguments + delta.partial_json,
                                )

                    case "content_block_stop":
                        current_tool_index = None

                    case "message_delta":
                        if hasattr(event, "delta") and hasattr(event.delta, "stop_reason"):
                            finish_reason = _map_stop_reason(event.delta.stop_reason)
                        if hasattr(event, "usage"):
                            usage = self._parse_usage(event.usage)

            # Get final message for complete usage
            final_message = await stream.get_final_message()
            if final_message and final_message.usage:
                usage = self._parse_usage(final_message.usage)
        finally:
            await stream_ctx.__aexit__(None, None, None)

        tool_calls_list = [tool_call_map[idx] for idx in sorted(tool_call_map)]

        # Mark tool calls with invalid JSON arguments as truncated
        for tc in tool_calls_list:
            try:
                json.loads(tc.arguments) if tc.arguments else None
            except json.JSONDecodeError:
                logger.warning(
                    "Tool call %r has invalid JSON arguments (truncated?): %s",
                    tc.name,
                    tc.arguments[:200],
                )
                tc.truncated = True

        self._append_assistant_message(full_text, full_thinking, tool_calls_list)

        yield StreamEvent(
            delta="",
            tool_calls=tool_calls_list,
            usage=usage,
            finish_reason=finish_reason,
        )
