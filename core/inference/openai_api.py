# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""OpenAI-compatible inference backend.

Implements InferenceProtocol using AsyncOpenAI client.
Configurable base_url supports vLLM, Together, OpenAI, etc.
"""

from __future__ import annotations

import asyncio
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
    ModelPricing,
    StreamEvent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolSchema,
    TurnResult,
)
from .protocol import PromptTooLongError, is_prompt_too_long_message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _retry_on_exception(
    exceptions: tuple,
    fn,
    *,
    kwargs: dict[str, Any],
    retries: int = 3,
    delay: float = 10.0,
):
    """Retry an async function on transient exceptions."""
    attempt = 0
    while True:
        try:
            return await fn(**kwargs)
        except exceptions as err:
            if attempt >= retries:
                logger.warning("Call %s failed; giving up: %s", fn.__name__, err)
                raise
            logger.warning(
                "Call %s failed; retrying %d/%d: %s",
                fn.__name__,
                attempt + 1,
                retries,
                err,
            )
            attempt += 1
            await asyncio.sleep(delay)


def _extract_thought_signature(obj: Any) -> str:
    """Extract Gemini thought_signature from a tool call object.

    In the OpenAI Chat Completions format, Gemini nests the signature
    under ``extra_content.google.thought_signature``.  The OpenAI SDK
    stores non-standard fields in ``model_extra``.
    """
    # Try model_extra first (Pydantic v2 stores unknown fields here)
    extras = getattr(obj, "model_extra", None) or {}
    extra_content = extras.get("extra_content")
    if isinstance(extra_content, dict):
        google = extra_content.get("google")
        if isinstance(google, dict):
            sig = google.get("thought_signature")
            if sig:
                return str(sig)
    # Direct attribute path (if SDK exposes nested objects)
    ec = getattr(obj, "extra_content", None)
    if ec:
        google = ec.get("google") if isinstance(ec, dict) else getattr(ec, "google", None)
        if google:
            sig = (
                google.get("thought_signature")
                if isinstance(google, dict)
                else getattr(google, "thought_signature", None)
            )
            if sig:
                return str(sig)
    # Dict path (for raw dicts)
    if isinstance(obj, dict):
        ec = obj.get("extra_content")
        if isinstance(ec, dict):
            google = ec.get("google")
            if isinstance(google, dict):
                return str(google.get("thought_signature", ""))
    return ""


def _build_tool_call(
    name: str,
    arguments: str,
    call_id: str,
    index: int,
    thought_signature: str = "",
) -> list[ToolCall]:
    """Validate and build a ToolCall from a single raw tool call entry.

    Returns a singleton list for consistency with the caller's extend() pattern.
    If the arguments are not valid JSON (indicating truncated output), the
    ToolCall is marked as truncated so the agent loop can skip execution.
    """
    truncated = False
    try:
        json.loads(arguments)
    except json.JSONDecodeError:
        logger.warning(
            "Tool call %r has invalid JSON arguments (truncated?): %s",
            name,
            arguments[:200],
        )
        truncated = True
    return [
        ToolCall(
            id=call_id or f"call_{index}",
            name=name,
            arguments=arguments,
            thought_signature=thought_signature,
            truncated=truncated,
        )
    ]


def _schemas_to_openai(tools: list[ToolSchema]) -> list[dict[str, Any]]:
    """Convert ToolSchema list to OpenAI function-tool format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _tool_results_to_messages(results: list[ToolResult]) -> list[dict[str, Any]]:
    """Convert ToolResult list to OpenAI tool-result messages."""
    return [
        {
            "role": "tool",
            "tool_call_id": r.tool_call_id,
            "content": r.content,
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def sanitize_accumulated_tool_calls(
    accumulated: dict[int, dict[str, str]],
    id_key: str = "id",
) -> list[ToolCall]:
    """Build ToolCall objects from accumulated streaming deltas.

    Validates JSON arguments, drops entries with empty names, and
    assigns fallback IDs when missing.

    Args:
        accumulated: Mapping of chunk index to ``{id_key, "name", "arguments"}``.
        id_key: Key used for the tool call ID (``"id"`` for Chat Completions,
            ``"call_id"`` for Response API).
    """
    tool_calls: list[ToolCall] = []
    for idx in sorted(accumulated):
        entry = accumulated[idx]
        tool_calls.extend(
            _build_tool_call(
                entry["name"],
                entry["arguments"] or "{}",
                entry.get(id_key, ""),
                idx,
                entry.get("thought_signature", ""),
            )
        )
    return tool_calls


async def retry_stream_create(
    create_fn,
    kwargs: dict[str, Any],
    *,
    retries: int = 3,
    delay: float = 10.0,
):
    """Retry a streaming create call before the first chunk is yielded.

    Handles transient errors and the "does not support tools" fallback.
    Returns the stream object on success.
    """
    attempt = 0
    while True:
        try:
            return await create_fn(**kwargs)
        except BadRequestError as err:
            if is_prompt_too_long_message(str(err)):
                raise PromptTooLongError(str(err)) from err
            if "does not support tools" in str(err).lower() and "tools" in kwargs:
                logger.warning("Model does not support tools; retrying without tools.")
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                return await create_fn(**kwargs)
            raise
        except (APITimeoutError, httpx.ReadTimeout, httpx.ConnectTimeout, RateLimitError, InternalServerError) as err:
            if attempt >= retries:
                raise
            logger.warning(
                "stream create failed; retrying %d/%d: %s",
                attempt + 1,
                retries,
                err,
            )
            attempt += 1
            await asyncio.sleep(delay)


class OpenAIInference(InferenceProtocol):
    """InferenceProtocol implementation using AsyncOpenAI.

    Stateful: owns the conversation history as a list of OpenAI-format
    message dicts.  The agent loop interacts via typed boundary methods.

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
        is_local: bool = False,
        pricing: ModelPricing | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._model_name = model_name
        self._retries = retries
        self._retry_delay = retry_delay
        self._is_local = is_local
        self._is_free_model = pricing is not None and pricing.is_free

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
        self._messages.extend(_tool_results_to_messages(results))

    def reset(self) -> None:
        self._messages.clear()

    def get_messages(self) -> list[dict[str, Any]]:
        msgs = list(self._messages)
        if self._system_prompt:
            msgs.insert(0, {"role": "system", "content": self._system_prompt})
        return msgs

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_messages_for_api(self) -> list[dict[str, Any]]:
        """Build the full message list for an API call (system + history)."""
        msgs: list[dict[str, Any]] = []
        if self._system_prompt:
            msgs.append({"role": "system", "content": self._system_prompt})
        msgs.extend(self._messages)
        return msgs

    def _append_assistant_message(self, text: str, thinking: str, tool_calls: list[ToolCall]) -> None:
        """Append an assistant message to internal history after a response."""
        msg: dict[str, Any] = {"role": "assistant", "content": text}
        if thinking:
            msg["thinking"] = thinking
        if tool_calls:
            tc_dicts: list[dict[str, Any]] = []
            for tc in tool_calls:
                tc_dict: dict[str, Any] = {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                if tc.thought_signature:
                    tc_dict["extra_content"] = {"google": {"thought_signature": tc.thought_signature}}
                tc_dicts.append(tc_dict)
            msg["tool_calls"] = tc_dicts
        self._messages.append(msg)

    @property
    def _is_local_provider(self) -> bool:
        """Whether this is a local provider that supports ``{"think": True}``."""
        return self._is_local

    @staticmethod
    def _extract_reasoning(obj: Any) -> str:
        """Extract thinking/reasoning content from a response object.

        Different providers use different field names for reasoning output:
        ``reasoning_content`` (DeepSeek/DashScope), ``reasoning`` (Ollama /v1,
        vLLM streaming deltas), ``thinking`` (Ollama native API).  Returns the
        first non-empty value found, or an empty string.
        """
        for field in ("reasoning_content", "reasoning", "thinking"):
            value = getattr(obj, field, None)
            if value:
                return value
        return ""

    @classmethod
    def _normalize_tool_calls(cls, tool_calls: Any) -> list[ToolCall]:
        """Normalize tool calls from various provider formats into ToolCall objects.

        Tool calls with empty names are kept (the runtime handles them as
        malformed calls). Invalid JSON arguments are replaced with ``{}``.
        """
        if not tool_calls:
            return []
        normalized: list[ToolCall] = []
        for idx, call in enumerate(tool_calls):
            if isinstance(call, dict):
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                arguments = str(fn.get("arguments") or "{}")
                call_id = str(call.get("id") or "")
                thought_sig = _extract_thought_signature(call)
            else:
                fn = getattr(call, "function", None)
                name = str(getattr(fn, "name", "") if fn else "")
                arguments = str(getattr(fn, "arguments", "{}") if fn else "{}")
                call_id = str(getattr(call, "id", ""))
                thought_sig = _extract_thought_signature(call)
            normalized.extend(_build_tool_call(name, arguments, call_id, idx, thought_sig))
        return normalized

    @staticmethod
    def _get_inference_params(config: InferenceConfig) -> dict[str, Any]:
        """Extract OpenAI-compatible API parameters from InferenceConfig."""
        params: dict[str, Any] = {}
        if config.temperature is not None:
            params["temperature"] = config.temperature
        if config.max_tokens is not None:
            params["max_tokens"] = config.max_tokens
        if config.top_p is not None:
            params["top_p"] = config.top_p
        return params

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
        messages = self._build_messages_for_api()

        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": messages,
            **self._get_inference_params(config),
        }
        openai_tools = _schemas_to_openai(tools) if tools else None
        if openai_tools:
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"
        if self._is_local_provider:
            kwargs["extra_body"] = {"think": True}

        try:
            response = await _retry_on_exception(
                (APITimeoutError, httpx.ReadTimeout, httpx.ConnectTimeout, RateLimitError, InternalServerError),
                self._client.chat.completions.create,
                kwargs=kwargs,
                retries=self._retries,
                delay=self._retry_delay,
            )
        except BadRequestError as err:
            if is_prompt_too_long_message(str(err)):
                raise PromptTooLongError(str(err)) from err
            if "does not support tools" in str(err).lower() and openai_tools:
                logger.warning("Model does not support tools; retrying without tools.")
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                response = await _retry_on_exception(
                    (APITimeoutError, httpx.ReadTimeout, httpx.ConnectTimeout, RateLimitError, InternalServerError),
                    self._client.chat.completions.create,
                    kwargs=kwargs,
                    retries=self._retries,
                    delay=self._retry_delay,
                )
            else:
                raise

        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason or ""
        reasoning = self._extract_reasoning(msg)
        content = msg.content or ""
        tool_calls = self._normalize_tool_calls(getattr(msg, "tool_calls", None))

        # Build usage
        usage = TokenUsage()
        if response.usage:
            cached = (
                getattr(
                    getattr(response.usage, "prompt_tokens_details", None),
                    "cached_tokens",
                    0,
                )
                or 0
            )
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                cached_input_tokens=cached,
                total_tokens=response.usage.total_tokens,
            )
        if not usage.total_tokens and not self._is_free_model:
            logger.warning("Provider did not report token usage for model %s", self._model_name)

        # Auto-append assistant message to internal history
        self._append_assistant_message(content, reasoning, tool_calls)

        return TurnResult(
            text=content,
            thinking=reasoning,
            tool_calls=tool_calls,
            usage=usage,
            model=response.model,
            call_id=getattr(response, "id", str(uuid.uuid4())),
            finish_reason=finish_reason,
        )

    # ------------------------------------------------------------------
    # Enhancements: compaction, recovery, streaming
    # ------------------------------------------------------------------

    def replace_history(self, summary: str) -> None:
        self._messages.clear()
        self._messages.append({"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"})

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [m for m in messages if m.get("role") != "system"]

    def cleanup_interrupted(self) -> None:
        while self._messages:
            last = self._messages[-1]
            role = last.get("role", "")
            if role == "tool":
                self._messages.pop()
                continue
            if role == "assistant":
                if last.get("tool_calls"):
                    self._messages.pop()
                    continue
                content = last.get("content", "")
                if not content or (isinstance(content, str) and not content.strip()):
                    self._messages.pop()
                    continue
            break

    async def stream(
        self,
        *,
        tools: list[ToolSchema] | None = None,
        inference_config: InferenceConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        config = inference_config or InferenceConfig()
        messages = self._build_messages_for_api()

        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self._get_inference_params(config),
        }
        openai_tools = _schemas_to_openai(tools) if tools else None
        if openai_tools:
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"
        if self._is_local_provider:
            kwargs["extra_body"] = {"think": True}

        # Retry only before any chunks are yielded
        response_stream = await retry_stream_create(
            self._client.chat.completions.create,
            kwargs,
            retries=self._retries,
            delay=self._retry_delay,
        )

        # Accumulate tool calls across chunks: {index: {id, name, arguments}}
        accumulated_tool_calls: dict[int, dict[str, str]] = {}
        full_content = ""
        full_thinking = ""
        usage = TokenUsage()
        finish_reason: str | None = None
        try:
            async for chunk in response_stream:
                choice = chunk.choices[0] if chunk.choices else None

                if choice:
                    delta = choice.delta
                    finish_reason = choice.finish_reason or finish_reason

                    # Text and thinking deltas (separated for reasoning models)
                    text = delta.content or "" if delta else ""
                    reasoning = self._extract_reasoning(delta) if delta else ""
                    if reasoning:
                        full_thinking += reasoning
                        yield StreamEvent(thinking=reasoning)
                    if text:
                        full_content += text
                        yield StreamEvent(delta=text)

                    # Tool call deltas
                    if delta and delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {
                                    "id": "",
                                    "name": "",
                                    "arguments": "",
                                    "thought_signature": "",
                                }
                            entry = accumulated_tool_calls[idx]
                            if tc_delta.id:
                                entry["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    entry["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    entry["arguments"] += tc_delta.function.arguments
                            # Gemini thinking models include thought_signature on tool calls
                            thought_sig = _extract_thought_signature(tc_delta)
                            if thought_sig:
                                entry["thought_signature"] = thought_sig

                # Usage (typically in the final chunk with choices=[])
                if chunk.usage:
                    cached = (
                        getattr(
                            getattr(chunk.usage, "prompt_tokens_details", None),
                            "cached_tokens",
                            0,
                        )
                        or 0
                    )
                    usage = TokenUsage(
                        input_tokens=getattr(chunk.usage, "prompt_tokens", 0) or 0,
                        output_tokens=getattr(chunk.usage, "completion_tokens", 0) or 0,
                        cached_input_tokens=cached,
                        total_tokens=getattr(chunk.usage, "total_tokens", 0) or 0,
                    )
        except (httpx.RemoteProtocolError, httpx.ReadError) as err:
            logger.warning("Stream interrupted: %s", err)
            # Discard partial tool calls — they likely have truncated arguments
            accumulated_tool_calls.clear()
            if full_content:
                full_content += "\n\n[Response was interrupted by a network error. Please retry if the response appears incomplete.]"

        # Build normalized ToolCall objects, sanitizing arguments JSON
        tool_calls_list = sanitize_accumulated_tool_calls(accumulated_tool_calls, id_key="id")

        if not usage.total_tokens and not self._is_free_model:
            logger.warning("Provider did not report token usage for model %s (streaming)", self._model_name)

        # Auto-append assistant message to internal history
        self._append_assistant_message(full_content, full_thinking, tool_calls_list)

        # Final event with usage and assembled tool calls
        yield StreamEvent(
            delta="",
            tool_calls=tool_calls_list,
            usage=usage,
            finish_reason=finish_reason or "stop",
        )
