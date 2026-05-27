# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tracing infrastructure for agent work.

Provides a base Trace class and AgentTrace for recording LLM calls,
tool calls, and conversation history.

Trace hierarchy:
- Trace (base) — common fields: trace_id, timestamps, status, finalize
  - AgentTrace — one agent's work: LLM calls, tool calls, messages
  - AgentTrace — a single agent's full execution record
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from core.inference import TokenUsage


@dataclass
class LLMCallRecord:
    """Record of a single LLM call."""

    call_id: str
    model: str
    timestamp: float
    latency_ms: float
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float | None = None

    # Exchange-aware fields (backward-compatible defaults)
    exchange_id: str | None = None
    sequence_num: int = 0  # 0 = initial call, 1+ = tool-loop iterations
    marginal_input_tokens: int | None = None  # tokens NEW to this call's input

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Flatten usage into top-level keys for backward compatibility
        usage = d.pop("usage", {})
        d["input_tokens"] = usage.get("input_tokens", 0)
        d["output_tokens"] = usage.get("output_tokens", 0)
        d["cached_input_tokens"] = usage.get("cached_input_tokens", 0)
        d["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0)
        d["total_tokens"] = usage.get("total_tokens", 0)
        return d


@dataclass
class ToolCallRecord:
    """Record of a single tool call with timing and result."""

    tool_name: str
    call_id: str
    start_time: float
    exchange_id: str | None = None
    end_time: float | None = None
    duration_ms: float | None = None
    success: bool = False
    error: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str | None = None

    def complete(self, *, success: bool, error: str | None = None, result: str | None = None) -> None:
        self.end_time = time.perf_counter()
        self.duration_ms = (self.end_time - self.start_time) * 1000
        self.success = success
        self.error = error
        self.result = result

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        status = "ok" if self.success else "FAIL"
        duration = f"{self.duration_ms:.1f}ms" if self.duration_ms else "?"
        return f"[{status}] {self.tool_name} ({duration})"


@dataclass
class DialogExchange:
    """One turn of user-assistant dialog: user asks, agent responds (possibly via tool loops)."""

    exchange_id: str
    user_message: str
    started_at: float
    ended_at: float | None = None

    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    def input_tokens(self) -> int:
        """Total input tokens billed across all LLM calls in this exchange."""
        return sum(c.usage.input_tokens for c in self.llm_calls)

    def output_tokens(self) -> int:
        """Total output tokens across all LLM calls in this exchange."""
        return sum(c.usage.output_tokens for c in self.llm_calls)

    def marginal_input_tokens(self) -> int:
        """Input tokens from tool-loop overhead (calls after the first).

        The first call's input is the full context (history + new question).
        Subsequent calls add tool results — that's the marginal overhead.
        """
        return sum(c.marginal_input_tokens or 0 for c in self.llm_calls if c.sequence_num > 0)

    def cost_usd(self) -> float:
        """Total cost across all LLM calls in this exchange."""
        return sum(c.cost_usd or 0.0 for c in self.llm_calls)

    def num_tool_loops(self) -> int:
        """Number of tool-loop iterations (LLM calls after the initial one)."""
        return max(0, len(self.llm_calls) - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange_id": self.exchange_id,
            "user_message": self.user_message,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "llm_calls": [c.to_dict() for c in self.llm_calls],
            "tool_calls": [c.to_dict() for c in self.tool_calls],
        }


@dataclass
class Trace:
    """Base trace with common fields shared by all trace types."""

    trace_id: str
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    prior_duration_s: float = 0.0
    final_status: str = "running"
    error: str | None = None

    def total_duration_s(self) -> float:
        if self.ended_at is None:
            return self.prior_duration_s + (time.time() - self.started_at)
        return self.prior_duration_s + (self.ended_at - self.started_at)

    def _finalize(self, *, status: str, error: str | None = None) -> None:
        """Set final status and timestamp. Subclasses call this."""
        self.ended_at = time.time()
        self.final_status = status
        self.error = error

    def _base_dict(self) -> dict[str, Any]:
        """Common fields for serialization. Subclasses extend this."""
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "prior_duration_s": self.prior_duration_s,
            "final_status": self.final_status,
            "error": self.error,
        }


@dataclass
class AgentTrace(Trace):
    """Trace of one agent's work.

    LLM and tool call records live exclusively inside DialogExchange objects.
    The ``llm_calls`` and ``tool_calls`` properties flatten them for convenient
    aggregate access.
    """

    id: str = ""
    total_turns: int = 0

    messages: list[dict[str, Any]] = field(default_factory=list)

    exchanges: list[DialogExchange] = field(default_factory=list)

    def __init__(self, id: str, task_id: str | None = None, **kwargs):
        self.id = id
        trace_id = f"{id}_{task_id}" if task_id else id
        super().__init__(trace_id=trace_id, **kwargs)
        self.total_turns = 0
        self.messages = []
        self.exchanges = []

    @property
    def llm_calls(self) -> list[LLMCallRecord]:
        return [c for ex in self.exchanges for c in ex.llm_calls]

    @property
    def tool_calls(self) -> list[ToolCallRecord]:
        return [c for ex in self.exchanges for c in ex.tool_calls]

    def finalize(
        self,
        *,
        status: str,
        total_turns: int,
        messages: list[dict[str, Any]],
        error: str | None = None,
    ) -> None:
        self._finalize(status=status, error=error)
        self.total_turns = total_turns
        self.messages = list(messages)

    def total_input_tokens(self) -> int:
        return sum(ex.input_tokens() for ex in self.exchanges)

    def total_output_tokens(self) -> int:
        return sum(ex.output_tokens() for ex in self.exchanges)

    def total_cached_input_tokens(self) -> int:
        return sum(c.usage.cached_input_tokens for ex in self.exchanges for c in ex.llm_calls)

    def total_cache_creation_input_tokens(self) -> int:
        return sum(c.usage.cache_creation_input_tokens for ex in self.exchanges for c in ex.llm_calls)

    def total_tokens(self) -> int:
        return sum(c.usage.total_tokens for ex in self.exchanges for c in ex.llm_calls)

    def total_cost_usd(self) -> float:
        return sum(ex.cost_usd() for ex in self.exchanges)

    def current_exchange(self) -> DialogExchange | None:
        """The most recent (possibly still-open) exchange."""
        return self.exchanges[-1] if self.exchanges else None

    def exchange_costs(self) -> list[dict[str, Any]]:
        """Per-exchange cost breakdown."""
        return [
            {
                "exchange_id": ex.exchange_id,
                "input_tokens": ex.input_tokens(),
                "output_tokens": ex.output_tokens(),
                "marginal_input_tokens": ex.marginal_input_tokens(),
                "cost_usd": round(ex.cost_usd(), 6),
                "num_tool_loops": ex.num_tool_loops(),
            }
            for ex in self.exchanges
        ]

    def tool_overhead_tokens(self) -> int:
        """Total marginal input tokens from tool-loop calls across all exchanges."""
        return sum(ex.marginal_input_tokens() for ex in self.exchanges)

    def summary(self) -> dict[str, Any]:
        successful_tools = sum(1 for t in self.tool_calls if t.success)
        failed_tools = sum(1 for t in self.tool_calls if not t.success)
        return {
            "total_turns": self.total_turns,
            "total_duration_s": round(self.total_duration_s(), 2),
            "num_llm_calls": len(self.llm_calls),
            "num_tool_calls": len(self.tool_calls),
            "successful_tool_calls": successful_tools,
            "failed_tool_calls": failed_tools,
            "total_input_tokens": self.total_input_tokens(),
            "total_output_tokens": self.total_output_tokens(),
            "total_cached_input_tokens": self.total_cached_input_tokens(),
            "total_cache_creation_input_tokens": self.total_cache_creation_input_tokens(),
            "total_tokens": self.total_tokens(),
            "total_cost_usd": round(self.total_cost_usd(), 4),
        }

    def to_dict(self) -> dict[str, Any]:
        d = self._base_dict()
        d.update(
            {
                "agent_id": self.id,
                "total_turns": self.total_turns,
                "llm_calls": [c.to_dict() for c in self.llm_calls],
                "tool_calls": [c.to_dict() for c in self.tool_calls],
                "exchanges": [ex.to_dict() for ex in self.exchanges],
                "messages": self.messages,
                "summary": self.summary(),
            }
        )
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentTrace:
        trace = cls(id=data.get("agent_id", ""))
        trace.trace_id = data.get("trace_id", data.get("agent_id", ""))
        trace.started_at = data.get("started_at", 0.0)
        trace.ended_at = data.get("ended_at")
        trace.prior_duration_s = data.get("prior_duration_s", 0.0)
        trace.total_turns = data.get("total_turns", 0)
        trace.final_status = data.get("final_status", "unknown")
        trace.error = data.get("error")
        trace.messages = data.get("messages", [])

        for ex_data in data.get("exchanges", []):
            ex = DialogExchange(
                exchange_id=ex_data.get("exchange_id", ""),
                user_message=ex_data.get("user_message", ""),
                started_at=ex_data.get("started_at", 0.0),
                ended_at=ex_data.get("ended_at"),
            )
            for c in ex_data.get("llm_calls", []):
                ex.llm_calls.append(_llm_record_from_dict(c))
            for c in ex_data.get("tool_calls", []):
                ex.tool_calls.append(_tool_record_from_dict(c))
            trace.exchanges.append(ex)

        # Backward compat: old traces have flat llm_calls/tool_calls but no exchanges.
        # Synthesize a single exchange so the properties work.
        if not trace.exchanges:
            flat_llm = [_llm_record_from_dict(c) for c in data.get("llm_calls", [])]
            flat_tool = [_tool_record_from_dict(c) for c in data.get("tool_calls", [])]
            if flat_llm or flat_tool:
                ex = DialogExchange(
                    exchange_id="legacy",
                    user_message="",
                    started_at=trace.started_at,
                    ended_at=trace.ended_at,
                )
                ex.llm_calls = flat_llm
                ex.tool_calls = flat_tool
                trace.exchanges.append(ex)

        return trace


def _llm_record_from_dict(c: dict[str, Any]) -> LLMCallRecord:
    """Deserialize an LLMCallRecord from a dict."""
    # Support both nested usage dict and flat token fields (backward compat)
    usage_data = c.get("usage", {})
    usage = TokenUsage(
        input_tokens=usage_data.get("input_tokens", 0) or c.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0) or c.get("output_tokens", 0),
        cached_input_tokens=usage_data.get("cached_input_tokens", 0) or c.get("cached_input_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0) or c.get("total_tokens", 0),
    )
    return LLMCallRecord(
        call_id=c.get("call_id", ""),
        model=c.get("model", ""),
        timestamp=c.get("timestamp", 0.0),
        latency_ms=c.get("latency_ms", 0.0),
        usage=usage,
        cost_usd=c.get("cost_usd"),
        exchange_id=c.get("exchange_id"),
        sequence_num=c.get("sequence_num", 0),
        marginal_input_tokens=c.get("marginal_input_tokens"),
    )


def _tool_record_from_dict(c: dict[str, Any]) -> ToolCallRecord:
    """Deserialize a ToolCallRecord from a dict."""
    return ToolCallRecord(
        tool_name=c.get("tool_name", ""),
        call_id=c.get("call_id", ""),
        start_time=c.get("start_time", 0.0),
        exchange_id=c.get("exchange_id"),
        end_time=c.get("end_time"),
        duration_ms=c.get("duration_ms"),
        success=c.get("success", False),
        error=c.get("error"),
        arguments=c.get("arguments", {}),
        result=c.get("result"),
    )
