# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Native Gemini SDK inference backend.

Implements InferenceProtocol using the google-genai Client.
Provides native function calling, thinking/reasoning support,
and proper token usage reporting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import types

from ..protocol import (
    InferenceConfig,
    InferenceProtocol,
    StreamEvent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolSchema,
    TurnResult,
)
from ..protocol import PromptTooLongError, is_prompt_too_long_message

logger = logging.getLogger(__name__)

# Transient error types from the google-genai SDK
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (TimeoutError, ConnectionError)
_REQUEST_ERRORS: tuple[type[Exception], ...] = ()
try:
    from google.api_core.exceptions import (
        DeadlineExceeded,
        InvalidArgument,
        ResourceExhausted,
        ServiceUnavailable,
    )

    _TRANSIENT_ERRORS = (*_TRANSIENT_ERRORS, ResourceExhausted, ServiceUnavailable, DeadlineExceeded)
    _REQUEST_ERRORS = (InvalidArgument,)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Format conversion helpers (internal)
# ---------------------------------------------------------------------------


def _schemas_to_gemini(tools: list[ToolSchema]) -> list[types.Tool]:
    """Convert ToolSchema list to Gemini tool format."""
    declarations = [
        types.FunctionDeclaration(
            name=t.name,
            description=t.description,
            parameters_json_schema=t.parameters,
        )
        for t in tools
    ]
    return [types.Tool(function_declarations=declarations)]


def _map_finish_reason(reason: Any) -> str:
    """Map Gemini finish reasons to normalized values."""
    reason_str = str(reason).upper() if reason else ""
    if "STOP" in reason_str:
        return "stop"
    if "MAX_TOKENS" in reason_str or "LENGTH" in reason_str:
        return "length"
    if "SAFETY" in reason_str:
        return "stop"
    # Function call responses don't always set a specific finish reason
    return "stop"


def _parse_usage(usage_metadata: Any) -> TokenUsage:
    """Extract TokenUsage from Gemini usage_metadata."""
    if not usage_metadata:
        return TokenUsage()
    input_tokens = getattr(usage_metadata, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage_metadata, "candidates_token_count", 0) or 0
    cached = getattr(usage_metadata, "cached_content_token_count", 0) or 0
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        total_tokens=input_tokens + output_tokens,
    )


class GeminiInference(InferenceProtocol):
    """InferenceProtocol implementation using the native google-genai Client.

    Stateful: owns the conversation history as a list of Gemini Content
    objects. The agent loop interacts via typed boundary methods.

    Args:
        client: google.genai.Client instance.
        retries: Number of retries on transient errors.
        retry_delay: Seconds between retries.
    """

    def __init__(
        self,
        client: genai.Client,
        *,
        model_name: str,
        retries: int = 3,
        retry_delay: float = 10.0,
    ) -> None:
        super().__init__()
        self._client = client
        self._model_name = model_name
        self._retries = retries
        self._retry_delay = retry_delay

        # Conversation state
        self._system_prompt: str = ""
        self._messages: list[types.Content] = []

    # ------------------------------------------------------------------
    # Protocol: conversation management
    # ------------------------------------------------------------------

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    def get_system_prompt(self) -> str:
        return self._system_prompt

    def add_user_message(self, content: str) -> None:
        self._messages.append(types.Content(role="user", parts=[types.Part.from_text(text=content)]))

    def add_tool_results(self, results: list[ToolResult]) -> None:
        parts = [
            types.Part.from_function_response(
                name=r.tool_name or r.tool_call_id,
                response={"result": r.content, "is_error": r.is_error},
            )
            for r in results
        ]
        self._messages.append(types.Content(role="user", parts=parts))

    def reset(self) -> None:
        self._messages.clear()

    def get_messages(self) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        if self._system_prompt:
            msgs.append({"role": "system", "content": self._system_prompt})
        for msg in self._messages:
            role = msg.role or "user"
            parts_data: list[dict[str, Any]] = []
            if msg.parts:
                for part in msg.parts:
                    if part.text is not None:
                        parts_data.append({"type": "text", "text": part.text})
                    elif part.function_call is not None:
                        parts_data.append(
                            {
                                "type": "function_call",
                                "name": part.function_call.name,
                                "args": part.function_call.args,
                            }
                        )
                    elif part.function_response is not None:
                        parts_data.append(
                            {
                                "type": "function_response",
                                "name": part.function_response.name,
                                "response": part.function_response.response,
                            }
                        )
                    elif part.thought:
                        parts_data.append({"type": "thinking", "text": part.text or ""})
                    else:
                        parts_data.append({"type": "unknown"})
            msgs.append({"role": role, "parts": parts_data})
        return msgs

    def replace_history(self, summary: str) -> None:
        self._messages.clear()
        self._messages.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"[Context summary of previous conversation]\n\n{summary}")],
            )
        )

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        raise NotImplementedError("replace_messages not yet supported for GeminiInference")

    def cleanup_interrupted(self) -> None:
        while self._messages:
            last = self._messages[-1]
            parts = last.parts or []
            if last.role == "user" and all(p.function_response is not None for p in parts):
                self._messages.pop()
                continue
            if last.role == "model":
                if any(p.function_call is not None for p in parts):
                    self._messages.pop()
                    continue
                has_text = any(p.text for p in parts)
                if not has_text:
                    self._messages.pop()
                    continue
            break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_config(
        self,
        config: InferenceConfig,
        tools: list[ToolSchema] | None,
    ) -> types.GenerateContentConfig:
        """Build GenerateContentConfig for the Gemini API."""
        kwargs: dict[str, Any] = {}

        if self._system_prompt:
            kwargs["system_instruction"] = self._system_prompt
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.max_tokens is not None:
            kwargs["max_output_tokens"] = config.max_tokens
        if config.top_p is not None:
            kwargs["top_p"] = config.top_p
        if tools:
            kwargs["tools"] = _schemas_to_gemini(tools)
            kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)

        return types.GenerateContentConfig(**kwargs)

    def _extract_response(self, response: Any) -> tuple[str, str, list[ToolCall], TokenUsage, str]:
        """Parse a Gemini response into (text, thinking, tool_calls, usage, finish_reason)."""
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        candidate = response.candidates[0] if response.candidates else None
        finish_reason = "stop"

        if candidate:
            if candidate.finish_reason:
                finish_reason = _map_finish_reason(candidate.finish_reason)

            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if part.function_call is not None:
                        tool_calls.append(
                            ToolCall(
                                id=f"call_{uuid.uuid4().hex[:24]}",
                                name=part.function_call.name,
                                arguments=json.dumps(dict(part.function_call.args) if part.function_call.args else {}),
                            )
                        )
                        finish_reason = "tool_calls"
                    elif part.thought:
                        thinking_parts.append(part.text or "")
                    elif part.text is not None:
                        text_parts.append(part.text)

        usage = _parse_usage(response.usage_metadata)
        text = "".join(text_parts)
        thinking = "".join(thinking_parts)
        return text, thinking, tool_calls, usage, finish_reason

    def _append_assistant_content(self, response: Any) -> None:
        """Append the model's response content to internal history."""
        candidate = response.candidates[0] if response.candidates else None
        if candidate and candidate.content:
            self._messages.append(candidate.content)

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
        gemini_config = self._build_config(config, tools)

        attempt = 0
        while True:
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model_name,
                    contents=self._messages,
                    config=gemini_config,
                )
                break
            except _REQUEST_ERRORS as err:
                if is_prompt_too_long_message(str(err)):
                    raise PromptTooLongError(str(err)) from err
                raise
            except _TRANSIENT_ERRORS as err:
                if attempt >= self._retries:
                    logger.warning("Gemini generate_content failed; giving up: %s", err)
                    raise
                logger.warning(
                    "Gemini generate_content failed; retrying %d/%d: %s",
                    attempt + 1,
                    self._retries,
                    err,
                )
                attempt += 1
                await asyncio.sleep(self._retry_delay)

        text, thinking, tool_calls, usage, finish_reason = self._extract_response(response)
        self._append_assistant_content(response)

        return TurnResult(
            text=text,
            thinking=thinking,
            tool_calls=tool_calls,
            usage=usage,
            model=self._model_name,
            call_id=f"gemini_{uuid.uuid4().hex[:12]}",
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
        config = inference_config or InferenceConfig()
        gemini_config = self._build_config(config, tools)

        attempt = 0
        while True:
            try:
                response_stream = await self._client.aio.models.generate_content_stream(
                    model=self._model_name,
                    contents=self._messages,
                    config=gemini_config,
                )
                break
            except _REQUEST_ERRORS as err:
                if is_prompt_too_long_message(str(err)):
                    raise PromptTooLongError(str(err)) from err
                raise
            except _TRANSIENT_ERRORS as err:
                if attempt >= self._retries:
                    raise
                logger.warning(
                    "Gemini stream failed; retrying %d/%d: %s",
                    attempt + 1,
                    self._retries,
                    err,
                )
                attempt += 1
                await asyncio.sleep(self._retry_delay)

        full_text = ""
        full_thinking = ""
        all_tool_calls: list[ToolCall] = []
        usage = TokenUsage()
        finish_reason = "stop"

        async for chunk in response_stream:
            # Extract deltas from this chunk
            chunk_text = ""
            chunk_thinking = ""

            candidate = chunk.candidates[0] if chunk.candidates else None
            if candidate:
                if candidate.finish_reason:
                    finish_reason = _map_finish_reason(candidate.finish_reason)

                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if part.function_call is not None:
                            all_tool_calls.append(
                                ToolCall(
                                    id=f"call_{uuid.uuid4().hex[:24]}",
                                    name=part.function_call.name,
                                    arguments=json.dumps(
                                        dict(part.function_call.args) if part.function_call.args else {}
                                    ),
                                )
                            )
                            finish_reason = "tool_calls"
                        elif part.thought:
                            chunk_thinking += part.text or ""
                        elif part.text is not None:
                            chunk_text += part.text

            if chunk.usage_metadata:
                usage = _parse_usage(chunk.usage_metadata)

            full_text += chunk_text
            full_thinking += chunk_thinking

            if chunk_text:
                yield StreamEvent(delta=chunk_text)
            if chunk_thinking:
                yield StreamEvent(thinking=chunk_thinking)

        # Append full assistant response to history
        # Build a synthetic Content from accumulated parts
        parts: list[types.Part] = []
        if full_thinking:
            parts.append(types.Part(text=full_thinking, thought=True))
        if full_text:
            parts.append(types.Part.from_text(text=full_text))
        for tc in all_tool_calls:
            parts.append(
                types.Part.from_function_call(
                    name=tc.name,
                    args=json.loads(tc.arguments),
                )
            )
        if parts:
            self._messages.append(types.Content(role="model", parts=parts))

        yield StreamEvent(
            delta="",
            tool_calls=all_tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )
