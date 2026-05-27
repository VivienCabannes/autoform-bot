# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""OpenAI Response API inference backend.

Implements InferenceProtocol using the OpenAI Response API
(client.responses.create) instead of Chat Completions.

Advantages over Chat Completions:
- Better prompt caching (40-80% improvement).
- Improved performance with reasoning models.
- Cleaner agentic tool-calling flow.

Uses manual context management: response output items are appended
back to the input list for subsequent turns, keeping conversation
state local (no previous_response_id).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import (
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from .protocol import (
    InferenceConfig,
    InferenceProtocol,
    StreamEvent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolSchema,
    TurnResult,
)
from .protocol import PromptTooLongError, is_prompt_too_long_message
from .openai_api import (
    _retry_on_exception,
    retry_stream_create,
    sanitize_accumulated_tool_calls,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format conversion helpers
# ---------------------------------------------------------------------------


def _schemas_to_response_api(
    tools: list[ToolSchema],
    *,
    nested: bool = False,
) -> list[dict[str, Any]]:
    """Convert ToolSchema list to Response API function-tool format.

    When *nested* is ``False`` (default / OpenAI native), uses the flat
    structure::

        {"type": "function", "name": ..., "description": ..., "parameters": ...}

    When *nested* is ``True``, uses a hybrid structure
    with both top-level ``name`` and a nested ``function`` object::

        {"type": "function", "name": ..., "function": {"name": ..., ...}}
    """
    if nested:
        return [
            {
                "type": "function",
                "name": t.name,
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in tools
    ]


def _tool_results_to_input_items(results: list[ToolResult]) -> list[dict[str, Any]]:
    """Convert ToolResult list to Response API function_call_output items."""
    return [
        {
            "type": "function_call_output",
            "call_id": r.tool_call_id,
            "output": r.content,
        }
        for r in results
    ]


class OpenAIResponseInference(InferenceProtocol):
    """InferenceProtocol implementation using the OpenAI Response API.

    Stateful: owns the conversation history as a list of Response API
    input items. The agent loop interacts via typed boundary methods.

    Args:
        client: AsyncOpenAI client (configure base_url for non-OpenAI endpoints).
        retries: Number of retries on transient errors.
        retry_delay: Seconds between retries.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model_name: str,
        retries: int = 3,
        retry_delay: float = 10.0,
        nested_tool_format: bool = False,
        disable_streaming: bool = False,
    ) -> None:
        self._client = client
        self._model_name = model_name
        self._retries = retries
        self._retry_delay = retry_delay
        self._nested_tool_format = nested_tool_format
        self._disable_streaming = disable_streaming

        super().__init__()

        # Conversation state
        self._instructions: str = ""
        self._messages: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Protocol: conversation management
    # ------------------------------------------------------------------

    def set_system_prompt(self, prompt: str) -> None:
        self._instructions = prompt

    def get_system_prompt(self) -> str:
        return self._instructions

    def add_user_message(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self._messages.extend(_tool_results_to_input_items(results))

    def reset(self) -> None:
        self._messages.clear()

    def get_messages(self) -> list[dict[str, Any]]:
        msgs = list(self._messages)
        if self._instructions:
            msgs.insert(0, {"role": "system", "content": self._instructions})
        return msgs

    def replace_history(self, summary: str) -> None:
        self._messages.clear()
        self._messages.append({"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"})

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [m for m in messages if m.get("role") != "system"]

    def cleanup_interrupted(self) -> None:
        while self._messages:
            last = self._messages[-1]
            item_type = last.get("type", "")
            if item_type == "function_call_output":
                self._messages.pop()
                continue
            if item_type == "function_call":
                self._messages.pop()
                continue
            role = last.get("role", "")
            if role == "assistant":
                content = last.get("content", "")
                if not content or (isinstance(content, str) and not content.strip()):
                    self._messages.pop()
                    continue
            break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_inference_params(config: InferenceConfig) -> dict[str, Any]:
        """Extract Response API-compatible parameters from InferenceConfig."""
        params: dict[str, Any] = {}
        if config.temperature is not None:
            params["temperature"] = config.temperature
        if config.max_tokens is not None:
            params["max_output_tokens"] = config.max_tokens
        if config.top_p is not None:
            params["top_p"] = config.top_p
        return params

    def _append_output_to_history(self, output_items: list[Any]) -> None:
        """Append response output items to input history for next turn.

        Strips ``status`` from serialized items — it is an output-only field
        that some providers reject as an unknown
        input parameter.
        """
        for item in output_items:
            if hasattr(item, "model_dump"):
                dumped = item.model_dump()
                dumped.pop("status", None)
                self._messages.append(dumped)
            elif isinstance(item, dict):
                cleaned = {k: v for k, v in item.items() if k != "status"}
                self._messages.append(cleaned)
            else:
                self._messages.append({"type": "unknown", "data": str(item)})

    @staticmethod
    def _extract_from_output(output_items: list[Any]) -> tuple[str, list[ToolCall], str]:
        """Extract text, tool calls, and reasoning from response output items.

        Returns:
            (text, tool_calls, reasoning) tuple.
        """
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        reasoning_parts: list[str] = []

        for item in output_items:
            item_type = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)

            if item_type == "message":
                # ResponseOutputMessage — extract text from content blocks
                content = getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else [])
                for block in content or []:
                    block_type = getattr(block, "type", None) or (
                        block.get("type") if isinstance(block, dict) else None
                    )
                    if block_type == "output_text":
                        text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
                        if text:
                            text_parts.append(text)
                    elif block_type == "refusal":
                        refusal = getattr(block, "refusal", None) or (
                            block.get("refusal") if isinstance(block, dict) else ""
                        )
                        if refusal:
                            text_parts.append(f"[Refusal: {refusal}]")

            elif item_type == "function_call":
                # ResponseFunctionToolCall
                call_id = getattr(item, "call_id", None) or (item.get("call_id") if isinstance(item, dict) else "")
                name = getattr(item, "name", None) or (item.get("name") if isinstance(item, dict) else "")
                arguments = getattr(item, "arguments", None) or (
                    item.get("arguments") if isinstance(item, dict) else "{}"
                )
                if name:
                    args_str = arguments or "{}"
                    truncated = False
                    try:
                        json.loads(args_str)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Tool call %r has invalid JSON arguments (truncated?): %s",
                            name,
                            args_str[:200],
                        )
                        truncated = True
                    tool_calls.append(
                        ToolCall(
                            id=call_id or f"call_{len(tool_calls)}",
                            name=name,
                            arguments=args_str,
                            truncated=truncated,
                        )
                    )

            elif item_type == "reasoning":
                # Reasoning output items (for reasoning models)
                summary = getattr(item, "summary", None) or (item.get("summary") if isinstance(item, dict) else [])
                for s in summary or []:
                    text = getattr(s, "text", None) or (s.get("text") if isinstance(s, dict) else "")
                    if text:
                        reasoning_parts.append(text)

        return "\n".join(text_parts), tool_calls, "\n".join(reasoning_parts)

    @staticmethod
    def _extract_usage(response: Any) -> TokenUsage:
        """Extract token usage from a Response object."""
        usage_obj = getattr(response, "usage", None)
        if not usage_obj:
            return TokenUsage()

        input_tokens = getattr(usage_obj, "input_tokens", 0) or 0
        output_tokens = getattr(usage_obj, "output_tokens", 0) or 0
        total_tokens = getattr(usage_obj, "total_tokens", 0) or 0

        cached = 0
        details = getattr(usage_obj, "input_tokens_details", None)
        if details:
            cached = getattr(details, "cached_tokens", 0) or 0

        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            total_tokens=total_tokens,
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

        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "input": list(self._messages),
            **self._get_inference_params(config),
        }
        if self._instructions:
            kwargs["instructions"] = self._instructions

        api_tools = _schemas_to_response_api(tools, nested=self._nested_tool_format) if tools else None
        if api_tools:
            kwargs["tools"] = api_tools
            kwargs["tool_choice"] = "auto"

        try:
            response = await _retry_on_exception(
                (APITimeoutError, httpx.ReadTimeout, httpx.ConnectTimeout, RateLimitError, InternalServerError),
                self._client.responses.create,
                kwargs=kwargs,
                retries=self._retries,
                delay=self._retry_delay,
            )
        except BadRequestError as err:
            if is_prompt_too_long_message(str(err)):
                raise PromptTooLongError(str(err)) from err
            if "does not support tools" in str(err).lower() and api_tools:
                logger.warning("Model does not support tools; retrying without tools.")
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                response = await _retry_on_exception(
                    (APITimeoutError, httpx.ReadTimeout, httpx.ConnectTimeout, RateLimitError, InternalServerError),
                    self._client.responses.create,
                    kwargs=kwargs,
                    retries=self._retries,
                    delay=self._retry_delay,
                )
            else:
                raise

        # Parse response
        output_items = getattr(response, "output", []) or []
        text, tool_calls, reasoning = self._extract_from_output(output_items)
        usage = self._extract_usage(response)

        # Append output items to history for next turn
        self._append_output_to_history(output_items)

        # Detect truncation via Response API status field
        if getattr(response, "status", None) == "incomplete":
            finish_reason = "length"
        else:
            finish_reason = "tool_calls" if tool_calls else "stop"

        return TurnResult(
            text=text,
            thinking=reasoning,
            tool_calls=tool_calls,
            usage=usage,
            model=getattr(response, "model", self._model_name),
            call_id=getattr(response, "id", str(uuid.uuid4())),
            finish_reason=finish_reason,
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
        if self._disable_streaming:
            # Fall back to non-streaming complete() + single event
            result = await self.complete(
                tools=tools,
                inference_config=inference_config,
            )
            yield StreamEvent(
                delta=result.text,
                tool_calls=result.tool_calls,
                usage=result.usage,
                finish_reason=result.finish_reason or ("tool_calls" if result.tool_calls else "stop"),
            )
            return

        config = inference_config or InferenceConfig()

        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "input": list(self._messages),
            "stream": True,
            **self._get_inference_params(config),
        }
        if self._instructions:
            kwargs["instructions"] = self._instructions

        api_tools = _schemas_to_response_api(tools, nested=self._nested_tool_format) if tools else None
        if api_tools:
            kwargs["tools"] = api_tools
            kwargs["tool_choice"] = "auto"

        # Retry only before any events are yielded
        event_stream = await retry_stream_create(
            self._client.responses.create,
            kwargs,
            retries=self._retries,
            delay=self._retry_delay,
        )

        # Accumulate state from events
        full_content = ""
        accumulated_tool_calls: dict[int, dict[str, str]] = {}  # output_index -> {call_id, name, arguments}
        usage = TokenUsage()
        completed_response = None

        try:
            async for event in event_stream:
                event_type = getattr(event, "type", "")

                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        full_content += delta
                        yield StreamEvent(delta=delta)

                elif event_type == "response.output_item.added":
                    # Track new function_call items by output_index
                    item = getattr(event, "item", None)
                    output_index = getattr(event, "output_index", 0)
                    if item and getattr(item, "type", None) == "function_call":
                        accumulated_tool_calls[output_index] = {
                            "call_id": getattr(item, "call_id", "") or "",
                            "name": getattr(item, "name", "") or "",
                            "arguments": "",
                        }

                elif event_type == "response.function_call_arguments.delta":
                    output_index = getattr(event, "output_index", 0)
                    delta = getattr(event, "delta", "")
                    if output_index in accumulated_tool_calls and delta:
                        accumulated_tool_calls[output_index]["arguments"] += delta

                elif event_type == "response.function_call_arguments.done":
                    output_index = getattr(event, "output_index", 0)
                    arguments = getattr(event, "arguments", "")
                    if output_index in accumulated_tool_calls and arguments:
                        accumulated_tool_calls[output_index]["arguments"] = arguments

                elif event_type == "response.completed":
                    completed_response = getattr(event, "response", None)
                    if completed_response:
                        usage = self._extract_usage(completed_response)

        except (httpx.RemoteProtocolError, httpx.ReadError) as err:
            logger.warning("Stream interrupted: %s", err)
            accumulated_tool_calls.clear()
            if full_content:
                full_content += "\n\n[Response was interrupted by a network error. Please retry if the response appears incomplete.]"

        # Build normalized ToolCall objects
        tool_calls_list = sanitize_accumulated_tool_calls(accumulated_tool_calls, id_key="call_id")

        # Append output to history from completed response
        if completed_response:
            output_items = getattr(completed_response, "output", []) or []
            self._append_output_to_history(output_items)
        else:
            # Fallback: reconstruct output items from accumulated state
            if full_content:
                self._messages.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": full_content}],
                    }
                )
            for idx in sorted(accumulated_tool_calls):
                entry = accumulated_tool_calls[idx]
                if entry["name"]:
                    self._messages.append(
                        {
                            "type": "function_call",
                            "call_id": entry["call_id"],
                            "name": entry["name"],
                            "arguments": entry["arguments"] or "{}",
                        }
                    )

        # Final event with usage and assembled tool calls
        finish_reason = "tool_calls" if tool_calls_list else "stop"
        yield StreamEvent(
            delta="",
            tool_calls=tool_calls_list,
            usage=usage,
            finish_reason=finish_reason,
        )
