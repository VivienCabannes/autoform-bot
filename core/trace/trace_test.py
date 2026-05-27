# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for core.trace."""

import time
from core.inference import TokenUsage
from core.trace import AgentTrace, DialogExchange, LLMCallRecord, ToolCallRecord
from core.trace.step_trace import step_trace_context


def test_agent_trace_creation():
    trace = AgentTrace(id="test-agent", task_id="target-1")
    assert trace.trace_id == "test-agent_target-1"
    assert trace.id == "test-agent"
    assert trace.final_status == "running"


def test_agent_trace_records():
    trace = AgentTrace(id="test")
    ex = DialogExchange(exchange_id="ex1", user_message="hi", started_at=time.time())
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c1",
            model="test",
            timestamp=time.time(),
            latency_ms=100,
            usage=TokenUsage(input_tokens=50, output_tokens=20, total_tokens=70),
            cost_usd=0.001,
        )
    )
    ex.tool_calls.append(
        ToolCallRecord(
            tool_name="test_tool",
            call_id="t1",
            start_time=time.perf_counter(),
        )
    )
    trace.exchanges.append(ex)
    assert len(trace.llm_calls) == 1
    assert len(trace.tool_calls) == 1
    assert trace.total_tokens() == 70
    assert trace.total_cost_usd() == 0.001


def test_agent_trace_serialization():
    trace = AgentTrace(id="test")
    ex = DialogExchange(exchange_id="ex1", user_message="hi", started_at=1.0)
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c1",
            model="m",
            timestamp=1.0,
            latency_ms=100,
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )
    )
    trace.exchanges.append(ex)
    trace.finalize(status="success", total_turns=3, messages=[{"role": "user", "content": "hi"}])

    d = trace.to_dict()
    assert d["agent_id"] == "test"
    assert d["final_status"] == "success"
    assert len(d["exchanges"]) == 1
    assert len(d["exchanges"][0]["llm_calls"]) == 1

    restored = AgentTrace.from_dict(d)
    assert restored.id == "test"
    assert restored.total_turns == 3
    assert len(restored.llm_calls) == 1


def test_dialog_exchange_aggregation():
    """DialogExchange correctly aggregates LLM call metrics."""
    ex = DialogExchange(exchange_id="ex1", user_message="hello", started_at=1.0)

    # Initial call: 100 input tokens
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c1",
            model="m",
            timestamp=1.0,
            latency_ms=50,
            usage=TokenUsage(input_tokens=100, output_tokens=30, total_tokens=130),
            cost_usd=0.01,
            exchange_id="ex1",
            sequence_num=0,
            marginal_input_tokens=100,
        )
    )
    # Tool-loop call: 150 input tokens (grew by tool results)
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c2",
            model="m",
            timestamp=2.0,
            latency_ms=60,
            usage=TokenUsage(input_tokens=150, output_tokens=20, total_tokens=170),
            cost_usd=0.008,
            exchange_id="ex1",
            sequence_num=1,
            marginal_input_tokens=20,
        )
    )

    assert ex.input_tokens() == 250
    assert ex.output_tokens() == 50
    assert ex.marginal_input_tokens() == 20  # only from sequence_num > 0
    assert abs(ex.cost_usd() - 0.018) < 1e-9
    assert ex.num_tool_loops() == 1


def test_marginal_input_computation():
    """Marginal formula: call n marginal = input[n] - input[n-1] - output[n-1]."""
    ex = DialogExchange(exchange_id="ex1", user_message="q", started_at=1.0)

    # Simulate 3 calls in a tool loop
    # Call 0: input=200 (all new)
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c0",
            model="m",
            timestamp=1.0,
            latency_ms=10,
            usage=TokenUsage(input_tokens=200, output_tokens=50, total_tokens=250),
            exchange_id="ex1",
            sequence_num=0,
            marginal_input_tokens=200,
        )
    )
    # Call 1: input=280 → marginal = 280 - 200 - 50 = 30 (tool result overhead)
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c1",
            model="m",
            timestamp=2.0,
            latency_ms=10,
            usage=TokenUsage(input_tokens=280, output_tokens=40, total_tokens=320),
            exchange_id="ex1",
            sequence_num=1,
            marginal_input_tokens=30,
        )
    )
    # Call 2: input=350 → marginal = 350 - 280 - 40 = 30
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c2",
            model="m",
            timestamp=3.0,
            latency_ms=10,
            usage=TokenUsage(input_tokens=350, output_tokens=25, total_tokens=375),
            exchange_id="ex1",
            sequence_num=2,
            marginal_input_tokens=30,
        )
    )

    assert ex.marginal_input_tokens() == 60  # 30 + 30 (excludes call 0)
    assert ex.num_tool_loops() == 2


def test_agent_trace_exchange_methods():
    """AgentTrace exchange-level methods work correctly."""
    trace = AgentTrace(id="test")

    assert trace.current_exchange() is None
    assert trace.exchange_costs() == []
    assert trace.tool_overhead_tokens() == 0

    ex = DialogExchange(exchange_id="ex1", user_message="hi", started_at=1.0, ended_at=2.0)
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c1",
            model="m",
            timestamp=1.0,
            latency_ms=10,
            usage=TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120),
            cost_usd=0.005,
            exchange_id="ex1",
            sequence_num=0,
            marginal_input_tokens=100,
        )
    )
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c2",
            model="m",
            timestamp=1.5,
            latency_ms=10,
            usage=TokenUsage(input_tokens=140, output_tokens=15, total_tokens=155),
            cost_usd=0.003,
            exchange_id="ex1",
            sequence_num=1,
            marginal_input_tokens=20,
        )
    )
    trace.exchanges.append(ex)

    assert trace.current_exchange() is ex
    assert trace.tool_overhead_tokens() == 20

    costs = trace.exchange_costs()
    assert len(costs) == 1
    assert costs[0]["exchange_id"] == "ex1"
    assert costs[0]["input_tokens"] == 240
    assert costs[0]["marginal_input_tokens"] == 20
    assert costs[0]["num_tool_loops"] == 1


def test_exchange_serialization_roundtrip():
    """Exchanges survive serialization and deserialization."""
    trace = AgentTrace(id="test")

    ex = DialogExchange(exchange_id="ex1", user_message="hello", started_at=1.0, ended_at=2.0)
    ex.llm_calls.append(
        LLMCallRecord(
            call_id="c1",
            model="m",
            timestamp=1.0,
            latency_ms=50,
            usage=TokenUsage(input_tokens=100, output_tokens=30, total_tokens=130),
            cost_usd=0.01,
            exchange_id="ex1",
            sequence_num=0,
            marginal_input_tokens=100,
        )
    )
    ex.tool_calls.append(
        ToolCallRecord(
            tool_name="read",
            call_id="t1",
            start_time=1.1,
            exchange_id="ex1",
            end_time=1.5,
            duration_ms=400,
            success=True,
        )
    )
    trace.exchanges.append(ex)

    trace.finalize(status="success", total_turns=1, messages=[])

    d = trace.to_dict()
    assert len(d["exchanges"]) == 1
    assert d["exchanges"][0]["exchange_id"] == "ex1"

    restored = AgentTrace.from_dict(d)
    assert len(restored.exchanges) == 1
    rex = restored.exchanges[0]
    assert rex.exchange_id == "ex1"
    assert rex.user_message == "hello"
    assert len(rex.llm_calls) == 1
    assert rex.llm_calls[0].sequence_num == 0
    assert rex.llm_calls[0].marginal_input_tokens == 100
    assert rex.llm_calls[0].exchange_id == "ex1"
    assert len(rex.tool_calls) == 1
    assert rex.tool_calls[0].exchange_id == "ex1"
    # Flat properties derive from exchanges
    assert len(restored.llm_calls) == 1
    assert len(restored.tool_calls) == 1


def test_backward_compat_no_exchanges():
    """Old trace JSON without exchanges synthesizes one from flat lists."""
    old_data = {
        "agent_id": "legacy",
        "trace_id": "legacy",
        "total_turns": 2,
        "final_status": "success",
        "llm_calls": [
            {
                "call_id": "c1",
                "model": "m",
                "timestamp": 1.0,
                "latency_ms": 100,
                "input_tokens": 50,
                "output_tokens": 20,
                "total_tokens": 70,
            }
        ],
        "tool_calls": [],
        "messages": [],
    }
    restored = AgentTrace.from_dict(old_data)
    # Flat lists synthesized into a single "legacy" exchange
    assert len(restored.exchanges) == 1
    assert restored.exchanges[0].exchange_id == "legacy"
    assert len(restored.llm_calls) == 1
    assert restored.llm_calls[0].exchange_id is None
    assert restored.llm_calls[0].sequence_num == 0
    assert restored.llm_calls[0].marginal_input_tokens is None
    # Existing functionality still works
    assert restored.total_input_tokens() == 50
    assert restored.total_output_tokens() == 20


def test_backward_compat_empty_flat_lists():
    """Old trace JSON with empty flat lists and no exchanges stays empty."""
    old_data = {
        "agent_id": "empty",
        "trace_id": "empty",
        "total_turns": 0,
        "final_status": "success",
        "llm_calls": [],
        "tool_calls": [],
        "messages": [],
    }
    restored = AgentTrace.from_dict(old_data)
    assert restored.exchanges == []
    assert restored.llm_calls == []
    assert restored.total_input_tokens() == 0


def test_step_trace_context():
    ctx = step_trace_context(trace_id="t1/attempt_1/steps")
    assert ctx.trace_id == "t1/attempt_1/steps"
    assert ctx.final_status == "running"
    assert ctx.winner_id is None

    ctx.winner_id = "agent-0"
    ctx.final_status = "success"
    d = ctx.to_dict()
    assert d["winner_id"] == "agent-0"
    assert d["final_status"] == "success"
    assert d["steps"] == []
