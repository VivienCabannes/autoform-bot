# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Inference protocol — abstract interface for LLM calls.

Defines the contract that all LLM backends must implement.
Concrete implementations live in inference/ (e.g., openai_api.py).
"""

from __future__ import annotations

import abc
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


DEFAULT_MAX_TOKENS: int = 16384


@dataclass(frozen=True)
class CacheConfig:
    """Controls prompt caching behavior across LLM backends.

    Anthropic-specific fields (explicit breakpoints):
        system, tools, messages — each adds one cache_control breakpoint.

    Backends ignore fields that don't apply to them.
    """

    # Anthropic: explicit cache breakpoints (max 4 per request).
    system: bool = True
    tools: bool = False
    messages: bool = False


@dataclass
class InferenceConfig:
    """Configuration for inference requests."""

    request_timeout: float = 60.0
    temperature: float | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    top_p: float | None = None
    cache: CacheConfig | None = None


@dataclass
class TokenUsage:
    """Token counts from a single LLM call.

    Attributes:
        input_tokens: Total input (prompt) tokens consumed, including
            cached reads and cache creation tokens.
        output_tokens: Number of output (completion) tokens generated.
        cached_input_tokens: Input tokens served from provider cache
            at a discounted rate. Subset of ``input_tokens``.
        cache_creation_input_tokens: Input tokens written to the
            provider cache (charged at a premium). Subset of ``input_tokens``.
        total_tokens: Sum of input and output tokens.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_tokens: int = 0


# ---------------------------------------------------------------------------
# Typed boundary types
# ---------------------------------------------------------------------------


@dataclass
class ToolSchema:
    """Backend-agnostic tool definition."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ToolCall:
    """A tool call requested by the model."""

    id: str
    name: str
    arguments: str  # JSON string
    thought_signature: str = ""  # Gemini thinking models require this round-tripped
    truncated: bool = False  # True when arguments are invalid JSON (likely truncated output)


@dataclass
class ToolResult:
    """Result of executing a tool call."""

    tool_call_id: str
    content: str
    tool_name: str = ""
    is_error: bool = False


@dataclass
class TurnResult:
    """Semantic output of one LLM turn."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    call_id: str = ""
    finish_reason: str = ""


# ---------------------------------------------------------------------------
# Model pricing
# ---------------------------------------------------------------------------


@dataclass
class ModelPricing:
    """Cost per million tokens for a model."""

    input_cost_per_m: float = 0.0  # $/M input tokens
    output_cost_per_m: float = 0.0  # $/M output tokens
    cached_input_cost_per_m: float | None = None  # $/M cached reads (None = same as input rate)
    cache_write_cost_per_m: float | None = None  # $/M cache writes (None = 1.25× input rate)

    @property
    def is_free(self) -> bool:
        return self.input_cost_per_m == 0.0 and self.output_cost_per_m == 0.0

    def compute_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> float:
        """Compute cost in USD for a given number of tokens.

        ``input_tokens`` is the *total* input count (regular + cached reads
        + cache writes).  The cached/creation subsets are charged at their
        respective discounted/premium rates.
        """
        read_rate = self.cached_input_cost_per_m if self.cached_input_cost_per_m is not None else self.input_cost_per_m
        write_rate = (
            self.cache_write_cost_per_m if self.cache_write_cost_per_m is not None else self.input_cost_per_m * 1.25
        )
        regular = input_tokens - cached_input_tokens - cache_creation_input_tokens
        return (
            regular * self.input_cost_per_m / 1_000_000
            + cached_input_tokens * read_rate / 1_000_000
            + cache_creation_input_tokens * write_rate / 1_000_000
            + output_tokens * self.output_cost_per_m / 1_000_000
        )

    @staticmethod
    def register(model: str, pricing: ModelPricing) -> None:
        """Register pricing for a model name.

        Called by provider modules (e.g. ``inference/client.py``) at import
        time so that ``core/config.py`` can look up pricing without an
        upward dependency.
        """
        _MODEL_PRICING_REGISTRY[model] = pricing


# Global registry — populated by provider modules via ModelPricing.register().
_MODEL_PRICING_REGISTRY: dict[str, ModelPricing] = {}


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@dataclass
class StreamEvent:
    """A single event from a streaming LLM response.

    Attributes:
        kind: Event type — "text" for LLM output, "thinking" for reasoning
              model thinking, "tool_start" when a tool-call turn begins,
              "tool_end" after tool execution completes, "usage" for token
              usage.  Empty string when unset (raw inference events before
              the agent loop tags them).
        delta: Text delta for "text" events, or empty string otherwise.
        thinking: Thinking delta for "thinking" events.
        tool_calls: Tool calls for "tool_start" events.
        tool_results: Tool results for "tool_end" events.
        usage: Token usage from the LLM call (for "usage" events).
        finish_reason: Stop reason from the LLM response.
    """

    kind: str = ""
    delta: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# Inference error types — part of the protocol contract
# ---------------------------------------------------------------------------


class PromptTooLongError(Exception):
    """Prompt exceeded the model's context window."""

    def __init__(
        self,
        message: str = "Prompt too long",
        *,
        input_tokens: int | None = None,
        max_tokens: int | None = None,
    ) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.max_tokens = max_tokens


_PROMPT_TOO_LONG_PATTERNS = re.compile(
    r"context.length|maximum.context|token.limit|too.many.tokens"
    r"|prompt.is.too.long|exceeds.*(?:context|token|limit)"
    r"|input.*too.long|request.too.large",
    re.IGNORECASE,
)


def is_prompt_too_long_message(message: str) -> bool:
    """Check if an error message indicates the prompt exceeded the context window."""
    return bool(_PROMPT_TOO_LONG_PATTERNS.search(message))


# ---------------------------------------------------------------------------
# Abstract protocol
# ---------------------------------------------------------------------------


class InferenceProtocol(abc.ABC):
    """Abstract interface for LLM inference backends.

    Stateful: owns the conversation history internally. Each backend
    stores messages in its native format. The agent loop interacts via
    typed boundary methods instead of manipulating raw message dicts.
    """

    _messages: list[Any]

    def __init__(self) -> None:
        self._messages = []

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """The API model name this backend sends requests to."""
        ...

    @abc.abstractmethod
    async def complete(
        self,
        *,
        tools: list[ToolSchema] | None = None,
        inference_config: InferenceConfig | None = None,
    ) -> TurnResult:
        """Make a single LLM completion call against internal history.

        Args:
            tools: Optional tool schemas.
            inference_config: Optional inference parameters.

        Returns:
            TurnResult with text, tool calls, and token usage.
        """
        ...

    async def stream(
        self,
        *,
        tools: list[ToolSchema] | None = None,
        inference_config: InferenceConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream an LLM response as incremental events.

        Default implementation calls complete() and yields a single event.
        Backends can override for true streaming.
        """
        result = await self.complete(tools=tools, inference_config=inference_config)
        yield StreamEvent(
            delta=result.text,
            tool_calls=result.tool_calls,
            usage=result.usage,
            finish_reason="stop" if not result.tool_calls else "tool_calls",
        )

    @abc.abstractmethod
    def add_user_message(self, content: str) -> None:
        """Append a user message to the conversation history."""
        ...

    @abc.abstractmethod
    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Append tool results to the conversation history."""
        ...

    @abc.abstractmethod
    def set_system_prompt(self, prompt: str) -> None:
        """Set or update the system prompt."""
        ...

    @abc.abstractmethod
    def get_system_prompt(self) -> str:
        """Return the current system prompt."""
        ...

    @abc.abstractmethod
    def reset(self) -> None:
        """Clear all conversation history (system prompt is preserved)."""
        ...

    @abc.abstractmethod
    def get_messages(self) -> list[dict[str, Any]]:
        """Return a serializable snapshot of the conversation for tracing.

        The format is backend-specific — callers should treat this as opaque.
        """
        ...

    @abc.abstractmethod
    def replace_history(self, summary: str) -> None:
        """Replace all non-system messages with a single user summary.

        Used for context compaction: after summarizing the conversation,
        the caller replaces the full history with the summary text.
        """
        ...

    @abc.abstractmethod
    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace the entire conversation history from a serialized snapshot.

        Used for restoring agent state from a saved trace. The messages
        should be in the same format returned by get_messages().
        """
        ...

    @abc.abstractmethod
    def cleanup_interrupted(self) -> None:
        """Remove trailing messages left in a broken state after interruption.

        Strips from the back: tool-result messages, assistant messages with
        pending tool calls, and empty assistant messages.  Stops at the
        first message that is safe to keep.

        Each backend must implement this for its own message format.
        """
        ...
