# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Agentic LLM with MCP tool support.

The base Agent class: runs an LLM + MCP tool loop.
Uses InferenceProtocol for LLM calls (decoupled from any specific backend).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .loader import AgentDefinition
from ..inference import (
    InferenceProtocol,
    PromptTooLongError,
    StreamEvent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolSchema,
    TurnResult,
    _MODEL_PRICING_REGISTRY,
)
from ..mcp import MCPClientManager, MCPServerConfig, MCPToolRuntime, SkillRegistry, ToolRegistry
from ..message_extract import extract_assistant, extract_text_content
from ..tool import ToolSpec
from ..resources import ResourcePool
from ..task import Task
from ..trace import AgentTrace, DialogExchange, LLMCallRecord

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant with access to tools."
DEFAULT_TRACE_MESSAGE_LIMIT = 500
DEFAULT_COMPACT_TRUNCATION = 2000
DEFAULT_TOOL_TIMEOUT = 30.0


class Agent(AgentDefinition):
    """Agentic LLM with MCP tool support.

    Inherits declarative fields (system_prompt, config, tool_allowlist,
    max_turns, tool_timeout_s, etc.) from AgentDefinition and adds
    runtime state (inference, MCP connections).

    Conversation history is owned by the inference protocol (stateful).
    """

    # -------------------------------------------------------------------------
    # Constructor
    # -------------------------------------------------------------------------

    def __init__(
        self,
        definition: AgentDefinition,
        inference: InferenceProtocol,
        server_configs: list[MCPServerConfig] | None = None,
        *,
        llm_semaphore: ResourcePool | None = None,
        tool_semaphores: dict[str, ResourcePool] | None = None,
        id: str | None = None,
        trace_store: Any | None = None,
        message_queue: asyncio.Queue[str] | None = None,
        persist_dir: Path | None = None,
        tool_registry: ToolRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        # Copy all AgentDefinition fields onto self
        for f in dataclasses.fields(definition):
            object.__setattr__(self, f.name, getattr(definition, f.name))

        self.inference = inference
        self.id = id or str(uuid.uuid4())
        self._llm_semaphore = llm_semaphore
        self._tool_semaphores = tool_semaphores or {}

        # Turn limits
        self.total_turns = 0

        # System prompt (will be enriched with tools context after init)
        self._base_system_prompt = self.system_prompt

        # Tracing (set by run() or set_trace())
        self._trace: AgentTrace | None = None
        self._trace_store = trace_store
        self._call_lock = asyncio.Lock()
        self._message_queue: asyncio.Queue[str] | None = message_queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_messages: list[str] = []
        self._queue_consumer: asyncio.Task | None = None

        # Tools configuration
        self._agent_tools = set(self.tool_allowlist)

        # MCP infrastructure
        self._mcp_manager: MCPClientManager | None = None
        self._mcp_runtime: MCPToolRuntime | None = None
        self._tools: list[ToolSchema] | None = None
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry

        if server_configs:
            self._mcp_manager = MCPClientManager(server_configs=server_configs)
            self._mcp_runtime = MCPToolRuntime(
                manager=self._mcp_manager,
                allowed_tools=list(self._agent_tools),
                tool_timeout_s=self.tool_timeout_s,
                persist_dir=persist_dir,
            )

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def __aenter__(self) -> Agent:
        """Start MCP servers and build system prompt with tools."""
        self._loop = asyncio.get_running_loop()
        self._idle_event = asyncio.Event()
        self._idle_event.set()  # agent starts idle
        if self._mcp_manager:
            await self._mcp_manager.discover_tools()

            # Populate tool registry for discovery tools
            if self._tool_registry:
                self._tool_registry.populate(self._mcp_manager)

            # Validate whitelist against discovered tools
            if self._agent_tools:
                discovered = set(self._mcp_manager.tool_to_server)
                unknown = self._agent_tools - discovered
                if unknown:
                    raise ValueError(
                        f"Whitelisted tools not found in any server: {sorted(unknown)}. "
                        f"Available tools: {sorted(discovered)}"
                    )

            # Validate explicit autonomy is not lower than tool-implied autonomy
            if self.autonomy is not None:
                implied = ToolSpec.compute_agent_autonomy(self.tool_allowlist)
                if self.autonomy.score < implied.score:
                    raise ValueError(
                        f"Explicit autonomy '{self.autonomy.value}' is lower than "
                        f"tool-implied autonomy '{implied.value}'"
                    )

            self._tools = await self._mcp_runtime.list_tools()
            self.system_prompt = self._build_system_prompt(self._tools)

        self.reset()

        # Start background queue consumer for interactive messages
        if self._message_queue:
            self._queue_consumer = asyncio.create_task(self._consume_queue())

        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the agent's MCP connections and stop the queue consumer."""
        if self._queue_consumer:
            self._queue_consumer.cancel()
            self._queue_consumer = None
        if self._mcp_manager:
            await self._mcp_manager.close_all()

    # -------------------------------------------------------------------------
    # Core API
    # -------------------------------------------------------------------------

    async def call(self, user_message: str | None = None) -> str:
        """Run the agent.

        If user_message is provided, add it first.
        Then loop: LLM call -> tool execution -> repeat until no tools.

        If another call is already in progress, waits for it to finish
        before starting (serialized via asyncio.Lock).

        Returns the assistant's final answer.
        """
        async with self._call_lock:
            self._idle_event.clear()
            try:
                if user_message:
                    self.inference.add_user_message(user_message)

                exchange = self._begin_exchange(user_message or "") if self._trace else None
                try:
                    result = await self._run_loop()
                    return result
                finally:
                    self._end_exchange(exchange)
            finally:
                self._idle_event.set()

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Serializable snapshot of conversation history (read-only).

        Delegates to the inference protocol's get_messages(). Returned
        list is a snapshot — mutating it has no effect on the protocol's
        internal state.
        """
        return self.inference.get_messages()

    def is_busy(self) -> bool:
        """True if the agent is currently executing a call."""
        return self._call_lock.locked()

    def reset(self) -> None:
        """Reset conversation to just the system prompt. Also resets turn counter."""
        self.inference.set_system_prompt(self.system_prompt)
        self.inference.reset()
        self.total_turns = 0

    def add_message(self, content: str) -> None:
        """Add a user message to conversation history without triggering a turn."""
        self.inference.add_user_message(content)

    def cleanup_interrupted(self) -> None:
        """Remove trailing broken messages after an interruption."""
        self.inference.cleanup_interrupted()

    def send_message(self, message: str) -> bool:
        """Enqueue an interactive message for the running agent.

        The message will be injected into the conversation at the next
        natural pause point, or processed as a new call if the agent
        is idle.

        Returns True if a queue is configured, False otherwise.
        """
        if not self._message_queue:
            return False
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._message_queue.put_nowait, message)
        else:
            self._message_queue.put_nowait(message)
        self._pending_messages.append(message)
        return True

    async def _consume_queue(self) -> None:
        """Background task that processes queued messages when idle.

        Waits for the agent to become idle (via _idle_event) before pulling
        from the queue. Messages that arrive mid-call are left for
        _drain_message_queue to pick up at the next turn boundary.
        """
        while True:
            try:
                await self._idle_event.wait()
                message = await self._message_queue.get()
                if message in self._pending_messages:
                    self._pending_messages.remove(message)
                await self.call(message)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in queue consumer")

    # -------------------------------------------------------------------------
    # Core internals
    # -------------------------------------------------------------------------

    _MAX_OUTPUT_RECOVERY = 3
    _MAX_REACTIVE_COMPACTIONS = 2

    async def _run_loop(
        self,
        tools: list[ToolSchema] | None = None,
        count_turns: bool = True,
    ) -> str:
        """Run the LLM + tool loop."""
        tools_to_use = tools if tools is not None else self._tools
        output_recovery_count = 0
        reactive_compaction_count = 0

        while not count_turns or self.total_turns < self.max_turns:
            if count_turns:
                self.total_turns += 1

            # Check for pending interactive messages before each LLM call
            pending = self._drain_message_queue()
            if pending:
                self.inference.add_user_message(pending)

            try:
                result = await self._call_llm(tools=tools_to_use)
            except PromptTooLongError:
                reactive_compaction_count += 1
                if reactive_compaction_count <= self._MAX_REACTIVE_COMPACTIONS:
                    logger.warning(
                        "Prompt too long — triggering reactive compaction (%d/%d)",
                        reactive_compaction_count,
                        self._MAX_REACTIVE_COMPACTIONS,
                    )
                    await self._compact()
                    continue
                raise

            # Detect truncated output — don't execute broken tool calls
            has_truncated = result.tool_calls and any(tc.truncated for tc in result.tool_calls)
            if result.tool_calls and (result.finish_reason == "length" or has_truncated):
                reason = "finish_reason=length" if result.finish_reason == "length" else "invalid JSON arguments"
                logger.warning("Output truncated (%s) — dropping %d tool calls", reason, len(result.tool_calls))
                dropped_names = [tc.name for tc in result.tool_calls]
                self.inference.add_tool_results(
                    [
                        ToolResult(
                            tool_call_id=tc.id,
                            content="Malformed arguments — tool call was not executed.",
                            tool_name=tc.name,
                            is_error=True,
                        )
                        for tc in result.tool_calls
                    ]
                )
                self.inference.add_user_message(
                    f"Your tool call(s) ({', '.join(dropped_names)}) had malformed arguments and could not be executed. "
                    "Please retry the same tool call(s) with valid, complete JSON arguments."
                )
                continue

            # Detect truncated text output — request continuation
            if result.finish_reason == "length" and not result.tool_calls:
                output_recovery_count += 1
                if output_recovery_count <= self._MAX_OUTPUT_RECOVERY:
                    logger.info(
                        "Output truncated (text only) — requesting continuation (%d/%d)",
                        output_recovery_count,
                        self._MAX_OUTPUT_RECOVERY,
                    )
                    self.inference.add_user_message(
                        "Your output was cut off due to token limits. "
                        "Continue exactly where you left off. "
                        "Do not repeat, apologize, or summarize."
                    )
                    continue
                return result.text

            output_recovery_count = 0

            if not result.tool_calls:
                if self._should_compact(result):
                    await self._compact()
                return result.text

            tool_results = await self._execute_tools(result.tool_calls)
            self.inference.add_tool_results(tool_results)

            # Compact after tool results so function_call/function_call_output
            # items are always paired — compacting between them orphans call_ids.
            if self._should_compact(result):
                await self._compact()

        logger.warning("Agent reached max_turns (%d) — stopping", self.max_turns)
        return ""

    def _drain_message_queue(self) -> str | None:
        """Drain all pending messages from the interactive queue.

        Returns a single combined message string, or None if the queue
        is empty. Multiple queued messages are joined with newlines.
        """
        if not self._message_queue:
            return None
        parts: list[str] = []
        while True:
            try:
                msg = self._message_queue.get_nowait()
                parts.append(msg)
            except asyncio.QueueEmpty:
                break
        if not parts:
            return None
        self._pending_messages.clear()
        return "\n\n".join(parts)

    async def _call_llm(self, tools: list[ToolSchema] | None = None) -> TurnResult:
        """Make a single LLM call and record it for tracing."""
        tools_to_use = tools if tools is not None else self._tools

        tracing = self._trace is not None
        if tracing:
            start_time = time.perf_counter()
            call_id = str(uuid.uuid4())

        async def do_call():
            return await self.inference.complete(
                tools=tools_to_use,
                inference_config=self.config.inference_config,
            )

        # Use semaphore if provided (for rate limiting)
        if self._llm_semaphore:
            async with self._llm_semaphore.acquire():
                result = await do_call()
        else:
            result = await do_call()

        # Record LLM call for tracing
        if tracing:
            self._record_llm_call(
                call_id=result.call_id or call_id,
                start_time=start_time,
                usage=result.usage,
                model=result.model,
            )

        return result

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """Execute tool calls and return tool results.

        If tool_semaphores are configured, each tool call is wrapped with
        the semaphore whose key is a prefix of the tool name.
        """
        if not self._mcp_runtime:
            return []

        if self._tool_semaphores:

            async def run_one(tc: ToolCall) -> list[ToolResult]:
                # Find a semaphore whose key is a prefix of the tool name (e.g. "bash" matches "bash_restricted").
                # If one matches, acquire it before executing to enforce concurrency limits.
                sem = next((s for p, s in self._tool_semaphores.items() if tc.name.startswith(p)), None)
                if sem:
                    async with sem.acquire():
                        return await self._mcp_runtime.execute([tc])
                return await self._mcp_runtime.execute([tc])

            tasks = [run_one(tc) for tc in tool_calls]
            nested = await asyncio.gather(*tasks)
            results = [r for batch in nested for r in batch]
            self._mcp_runtime.apply_aggregate_budget(results)
        else:
            results = await self._mcp_runtime.execute(tool_calls)

        if self._trace and self._trace_store:
            self._trace.messages = self.inference.get_messages()
            self._trace.total_turns = self.total_turns
            self._trace_store.save(self._trace)
        return results

    def _build_system_prompt(self, tools: list[ToolSchema] | None) -> str:
        """Build system prompt with tools and skills context."""
        lines = [self._base_system_prompt]

        # ── Tools section ─────────────────────────────────────────
        if tools and self._tool_registry and self._tool_registry.servers:
            summary = self._tool_registry.format_compact_summary()
            lines.extend(
                [
                    "",
                    "## Available tools",
                    "",
                    "Use `list_tools()` to see available tool collections "
                    "and `check_tools(name)` for detailed usage documentation.",
                    "",
                    summary,
                ]
            )
        elif tools:
            lines.extend(["", "## Available tools"])
            for tool in tools:
                name = tool.name
                desc = tool.description.strip()
                if not desc:
                    lines.append(f"\n- {name}")
                    continue
                parts = desc.split("\n\n", 1)
                summary = parts[0].replace("\n", " ")
                lines.append(f"\n- {name}: {summary}")
                if len(parts) > 1:
                    lines.append(f"\n```\n{parts[1]}\n```")

        # ── Skills section ────────────────────────────────────────
        if self._skill_registry and self._skill_registry.skills:
            skill_summary = self._skill_registry.format_compact_summary()
            lines.extend(
                [
                    "",
                    "## Available skills",
                    "",
                    "Use `list_skills()` to see available skills and `check_skills(name)` for the full skill content.",
                    "",
                    skill_summary,
                ]
            )

        return "\n".join(lines)

    # =========================================================================
    # Enhancements
    # =========================================================================

    # -------------------------------------------------------------------------
    # Streaming
    # -------------------------------------------------------------------------

    async def call_streaming(self, user_message: str | None = None) -> AsyncIterator[StreamEvent]:
        """Run the agent, yielding ``StreamEvent`` objects as they stream in.

        Each event has a ``kind`` ("text", "tool_start", "tool_end") and data.
        If another call is in progress, waits for it to finish first.
        """
        await self._call_lock.acquire()
        self._idle_event.clear()
        try:
            if user_message:
                self.inference.add_user_message(user_message)

            exchange = self._begin_exchange(user_message or "") if self._trace else None
            try:
                async for event in self._run_loop_streaming():
                    yield event
            finally:
                self._end_exchange(exchange)
        finally:
            self._idle_event.set()
            self._call_lock.release()

    async def _run_loop_streaming(self) -> AsyncIterator[StreamEvent]:
        """Streaming variant of _run_loop.

        Yields ``StreamEvent`` objects so callers can render text and tool
        activity differently.  Mirrors the recovery logic of ``_run_loop``:

        - ``PromptTooLongError`` triggers reactive compaction.
        - Truncated tool calls (``finish_reason == "length"`` with tool_calls)
          are dropped and the model is asked to retry with smaller steps.
        - Truncated text (``finish_reason == "length"`` without tool_calls)
          triggers a continuation request.
        """
        tools_to_use = self._tools
        retried_empty = False
        output_recovery_count = 0
        reactive_compaction_count = 0

        while self.total_turns < self.max_turns:
            self.total_turns += 1

            # Check for pending interactive messages before each LLM call
            pending = self._drain_message_queue()
            if pending:
                self.inference.add_user_message(pending)

            # Stream the LLM call — yield text deltas in real-time
            full_content = ""
            full_thinking = ""
            tool_calls: list[ToolCall] = []
            usage = None
            finish_reason: str | None = None
            tracing = self._trace is not None
            if tracing:
                start_time = time.perf_counter()
                call_id = str(uuid.uuid4())

            async def do_stream():
                return self.inference.stream(
                    tools=tools_to_use,
                    inference_config=self.config.inference_config,
                )

            sem_ctx = self._llm_semaphore.acquire() if self._llm_semaphore else contextlib.nullcontext()
            try:
                async with sem_ctx:
                    stream_iter = await do_stream()
                    async for event in stream_iter:
                        if event.thinking:
                            full_thinking += event.thinking
                            yield StreamEvent(kind="thinking", thinking=event.thinking)
                        if event.delta:
                            full_content += event.delta
                            yield StreamEvent(kind="text", delta=event.delta)
                        if event.tool_calls:
                            tool_calls = event.tool_calls
                        if event.usage:
                            usage = event.usage
                        if event.finish_reason is not None:
                            finish_reason = event.finish_reason
            except PromptTooLongError:
                reactive_compaction_count += 1
                if reactive_compaction_count <= self._MAX_REACTIVE_COMPACTIONS:
                    logger.warning(
                        "Prompt too long (streaming) — triggering reactive compaction (%d/%d)",
                        reactive_compaction_count,
                        self._MAX_REACTIVE_COMPACTIONS,
                    )
                    await self._compact()
                    continue
                raise

            # Record tracing
            if tracing:
                self._record_llm_call(call_id=call_id, start_time=start_time, usage=usage)

            # Emit usage event so callers can track cost
            if usage:
                yield StreamEvent(kind="usage", usage=usage)

            # Note: assistant message was already appended by inference.stream()

            # --- Truncation recovery (mirrors _run_loop) ---

            # Truncated tool calls: drop them and ask model to retry.
            # Safe because tool_start has not been yielded yet.
            has_truncated = tool_calls and any(tc.truncated for tc in tool_calls)
            if tool_calls and (finish_reason == "length" or has_truncated):
                reason = "finish_reason=length" if finish_reason == "length" else "invalid JSON arguments"
                logger.warning(
                    "Output truncated (%s, streaming) — dropping %d tool calls",
                    reason,
                    len(tool_calls),
                )
                dropped_names = [tc.name for tc in tool_calls]
                self.inference.add_tool_results(
                    [
                        ToolResult(
                            tool_call_id=tc.id,
                            content="Malformed arguments — tool call was not executed.",
                            tool_name=tc.name,
                            is_error=True,
                        )
                        for tc in tool_calls
                    ]
                )
                self.inference.add_user_message(
                    f"Your tool call(s) ({', '.join(dropped_names)}) had malformed arguments and could not be executed. "
                    "Please retry the same tool call(s) with valid, complete JSON arguments."
                )
                continue

            # Truncated text: request continuation.
            if finish_reason == "length" and not tool_calls:
                output_recovery_count += 1
                if output_recovery_count <= self._MAX_OUTPUT_RECOVERY:
                    logger.info(
                        "Output truncated (text only, streaming) — requesting continuation (%d/%d)",
                        output_recovery_count,
                        self._MAX_OUTPUT_RECOVERY,
                    )
                    self.inference.add_user_message(
                        "Your output was cut off due to token limits. "
                        "Continue exactly where you left off. "
                        "Do not repeat, apologize, or summarize."
                    )
                    continue
                return

            output_recovery_count = 0

            # Check if compaction is needed (for non-tool responses)
            if usage and self._should_compact_from_usage(usage.input_tokens):
                if not tool_calls:
                    await self._compact()

            # Handle empty response (no text, no thinking, no tool calls)
            if not full_content and not full_thinking and not tool_calls:
                if self.total_turns > 1 and not retried_empty:
                    retried_empty = True
                    continue
                yield StreamEvent(kind="text", delta="(empty response from model)")
                return

            if not tool_calls:
                return

            # Intermediate turn — emit tool indicator, execute tools
            yield StreamEvent(kind="tool_start", tool_calls=tool_calls)
            tool_results = await self._execute_tools(tool_calls)
            self.inference.add_tool_results(tool_results)
            yield StreamEvent(kind="tool_end", tool_results=tool_results)

            # Compact after tool results so function_call/function_call_output
            # items are always paired — compacting between them orphans call_ids.
            if usage and self._should_compact_from_usage(usage.input_tokens):
                await self._compact()

        logger.warning("Agent reached max_turns (%d) — stopping", self.max_turns)

    # -------------------------------------------------------------------------
    # Task runner
    # -------------------------------------------------------------------------

    async def run(self, task: Task) -> tuple[bool, AgentTrace]:
        """Run the agent on a task to completion.

        Manages the full lifecycle: enter context -> reset -> create trace ->
        call with task prompt -> finalize trace -> exit context.

        Returns:
            (success, trace) — success is True if the agent completed without error.
        """
        if self._trace_store:
            trace = AgentTrace(id=self.id, task_id=task.id)
            self.set_trace(trace)
        else:
            trace = None

        error: str | None = None
        status = "success"

        async with self:
            self.reset()
            try:
                await self.call(task.description)
            except Exception as e:
                status = "failed"
                error = str(e)
            finally:
                if trace:
                    trace.finalize(
                        status=status,
                        total_turns=self.total_turns,
                        messages=self.inference.get_messages(),
                        error=error,
                    )
                    self._trace_store.save(trace)

        # Return a minimal trace when no store is configured
        if trace is None:
            trace = AgentTrace(id=self.id, task_id=task.id)
            trace.status = status
            trace.error = error

        return status == "success", trace

    # -------------------------------------------------------------------------
    # Tracing / exchange lifecycle
    # -------------------------------------------------------------------------

    _current_exchange: DialogExchange | None = None
    _exchange_seq: int = 0  # sequence_num counter within an exchange
    _prev_usage: TokenUsage | None = None  # previous call's usage for marginal calc

    def set_trace(self, trace: AgentTrace | None) -> None:
        """Set the trace for recording LLM and tool calls."""
        self._trace = trace
        if self._mcp_runtime:
            self._mcp_runtime.set_trace(trace)

    def finalize_trace(self, status: str = "completed", error: str | None = None) -> None:
        """Finalize and save the agent's trace if one is active."""
        if self._trace is None:
            return
        self._trace.finalize(
            status=status,
            total_turns=self.total_turns,
            messages=self.messages,
            error=error,
        )
        if self._trace_store:
            self._trace_store.save(self._trace)

    def load_from_trace(self, trace_data: dict) -> None:
        """Restore agent state from a saved trace dict.

        Populates messages, turn counter, and the full AgentTrace
        so that subsequent calls continue from where the previous run
        left off. The system prompt is taken from the current agent
        config, not from the saved trace.
        """
        messages = trace_data.get("messages", [])
        self.total_turns = trace_data.get("total_turns", 0)
        self.inference.replace_messages(messages)
        self.inference.cleanup_interrupted()
        restored = AgentTrace.from_dict(trace_data)
        restored.messages = self.inference.get_messages()
        restored.started_at = time.time()
        restored.ended_at = None
        restored.final_status = "running"
        self.set_trace(restored)

    def _begin_exchange(self, user_message: str) -> DialogExchange:
        """Create a new DialogExchange and register it on the trace."""
        exchange_id = str(uuid.uuid4())
        exchange = DialogExchange(
            exchange_id=exchange_id,
            user_message=user_message[:DEFAULT_TRACE_MESSAGE_LIMIT],
            started_at=time.time(),
        )
        if self._trace:
            self._trace.exchanges.append(exchange)
        if self._mcp_runtime:
            self._mcp_runtime.set_exchange_id(exchange_id)
        self._current_exchange = exchange
        self._exchange_seq = 0
        self._prev_usage = None
        return exchange

    def _end_exchange(self, exchange: DialogExchange | None) -> None:
        """Close a DialogExchange. No-op when exchange is None (no-trace case)."""
        if exchange is None:
            return
        exchange.ended_at = time.time()
        self._current_exchange = None
        if self._mcp_runtime:
            self._mcp_runtime.set_exchange_id(None)

    def _compute_marginal(self, usage: TokenUsage) -> int:
        """Compute marginal input tokens for the current call in an exchange."""
        if self._exchange_seq == 0:
            return usage.input_tokens
        if self._prev_usage:
            return max(0, usage.input_tokens - self._prev_usage.input_tokens - self._prev_usage.output_tokens)
        return usage.input_tokens

    def _record_llm_call(
        self,
        *,
        call_id: str,
        start_time: float,
        usage: TokenUsage | None,
        model: str | None = None,
    ) -> None:
        """Record an LLM call in the trace. No-op when tracing is off or usage is None."""
        if not self._trace or not usage:
            return
        latency_ms = (time.perf_counter() - start_time) * 1000
        exchange = self._current_exchange
        exchange_id = exchange.exchange_id if exchange else None
        marginal = self._compute_marginal(usage) if exchange else None
        actual_model = model or self.inference.model_name
        pricing = _MODEL_PRICING_REGISTRY.get(actual_model, self.config.pricing)
        cost = pricing.compute_cost(
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_input_tokens,
            usage.cache_creation_input_tokens,
        )
        record = LLMCallRecord(
            call_id=call_id,
            model=actual_model,
            timestamp=time.time(),
            latency_ms=latency_ms,
            usage=usage,
            cost_usd=cost,
            exchange_id=exchange_id,
            sequence_num=self._exchange_seq,
            marginal_input_tokens=marginal,
        )
        if exchange:
            exchange.llm_calls.append(record)
            self._prev_usage = usage
            self._exchange_seq += 1
        if self._trace_store:
            self._trace.messages = self.inference.get_messages()
            self._trace.total_turns = self.total_turns
            self._trace_store.save(self._trace)

    # -------------------------------------------------------------------------
    # Context compaction
    # -------------------------------------------------------------------------

    @property
    def _compact_token_limit(self) -> int | None:
        """Token threshold that triggers compaction, or None if disabled."""
        if self.config.context_window is None:
            return None
        return int(self.config.context_window * self.config.compact_threshold)

    def _should_compact(self, result: TurnResult) -> bool:
        """Check whether compaction is needed based on a TurnResult."""
        return self._should_compact_from_usage(result.usage.input_tokens)

    def _should_compact_from_usage(self, input_tokens: int) -> bool:
        """Check whether compaction is needed based on input token count."""
        limit = self._compact_token_limit
        if limit is None:
            return False
        return input_tokens > limit

    async def compact(self) -> int:
        """Compact the conversation history by summarizing older messages.

        Returns the number of messages removed.
        """
        before = len(self.inference.get_messages())
        await self._compact()
        after = len(self.inference.get_messages())
        return before - after

    async def _compact(self) -> None:
        """Summarize all non-system messages into a single user summary.

        Uses replace_history() on the inference protocol to swap out the
        full conversation with a summary. If the last message has pending
        tool_calls, it is preserved and re-appended after the summary
        to maintain the toolUse/toolResult pairing required by LLM APIs.
        """
        messages = self.inference.get_messages()
        to_summarize = messages[1:]  # skip system message
        if not to_summarize:
            return

        before_count = len(messages)

        # Check for pending tool_calls on the last message.
        # If present, exclude it from summarization and re-append it
        # after the summary so that subsequent tool result messages
        # have a matching assistant turn.
        last_msg = to_summarize[-1]
        pending_assistant = None
        if last_msg.get("role") in ("assistant", "model"):
            extracted = extract_assistant(last_msg)
            if extracted.tool_calls:
                pending_assistant = last_msg
                to_summarize = to_summarize[:-1]
                if not to_summarize:
                    return

        logger.info(
            "Compacting %d messages (pending_tool_calls=%s)",
            before_count,
            bool(pending_assistant),
        )

        # Build summary prompt
        summary_lines = []
        for msg in to_summarize:
            role = msg.get("role", "unknown")
            if role in ("assistant", "model"):
                extracted = extract_assistant(msg)
                if extracted.tool_calls:
                    tool_names = [tc.name for tc in extracted.tool_calls]
                    summary_lines.append(f"[assistant called tools: {', '.join(tool_names)}]")
                if extracted.text:
                    content = extracted.text
                    if len(content) > DEFAULT_COMPACT_TRUNCATION:
                        content = content[:DEFAULT_COMPACT_TRUNCATION] + "... [truncated]"
                    summary_lines.append(f"[assistant]: {content}")
            elif role == "tool":
                content = extract_text_content(msg)
                if len(content) > DEFAULT_COMPACT_TRUNCATION:
                    content = content[:DEFAULT_COMPACT_TRUNCATION] + "... [truncated]"
                tool_name = msg.get("name", "unknown")
                summary_lines.append(f"[tool result from {tool_name}]: {content}")
            else:
                content = extract_text_content(msg)
                if len(content) > DEFAULT_COMPACT_TRUNCATION:
                    content = content[:DEFAULT_COMPACT_TRUNCATION] + "... [truncated]"
                summary_lines.append(f"[{role}]: {content}")

        conversation_text = "\n".join(summary_lines)

        # Snapshot state before modifying — restore on failure to prevent corruption
        saved_system = self.inference.get_system_prompt()
        saved_messages = list(to_summarize)
        if pending_assistant:
            saved_messages.append(pending_assistant)

        self.inference.set_system_prompt(
            "You are a summarizer. Produce a concise summary of the conversation below. "
            "Focus on: key findings, decisions made, actions taken, and remaining work. "
            "Be specific about important details like file paths, names, and values."
        )
        self.inference.reset()
        self.inference.add_user_message(f"Summarize this conversation:\n\n{conversation_text}")

        try:
            result = await self.inference.complete(
                tools=[],
                inference_config=self.config.inference_config,
            )
        except Exception:
            logger.warning("Compaction LLM call failed — restoring original state")
            self.inference.set_system_prompt(saved_system)
            self.inference.reset()
            self.inference.replace_messages(saved_messages)
            raise

        summary_text = result.text

        # Restore system prompt and replace history with summary
        self.inference.set_system_prompt(saved_system)
        self.inference.replace_history(summary_text)

        # Re-append pending assistant message with tool_calls
        if pending_assistant:
            self.inference.replace_messages(self.inference.get_messages()[1:] + [pending_assistant])

        logger.info("Compaction complete: %d → %d messages", before_count, len(self.inference.get_messages()))

    # -------------------------------------------------------------------------
    # Dynamic tool switching
    # -------------------------------------------------------------------------

    async def refresh_tools(self) -> None:
        """Re-discover tools from all MCP servers and rebuild the tool list."""
        if self._mcp_manager:
            await self._mcp_manager.discover_tools()
            if self._tool_registry:
                self._tool_registry.populate(self._mcp_manager)
            if self._mcp_runtime:
                self._tools = await self._mcp_runtime.list_tools()
                self.system_prompt = self._build_system_prompt(self._tools)
                self.inference.set_system_prompt(self.system_prompt)

    async def set_agent_tools(self, tools: list[str]) -> None:
        """Update the agent's allowed tools at runtime.

        Rebuilds the tool list, system prompt, and updates the system message.
        """
        self._agent_tools = set(tools)
        if self._mcp_runtime:
            self._mcp_runtime.set_allowed_tools(tools)
            self._tools = await self._mcp_runtime.list_tools()
            self.system_prompt = self._build_system_prompt(self._tools)
            self.inference.set_system_prompt(self.system_prompt)

    def set_base_system_prompt(self, prompt: str) -> None:
        """Update the base system prompt and rebuild the full prompt with tools."""
        self._base_system_prompt = prompt
        self.system_prompt = self._build_system_prompt(self._tools)
        self.inference.set_system_prompt(self.system_prompt)

    def get_tool_info(self) -> dict:
        """Return tool discovery info for display purposes.

        Returns a dict with:
            - server_to_tools: mapping of server name -> set of tool names
            - tool_to_server: mapping of tool name -> server name
            - allowed_tools: set of currently allowed tool names
            - all_discovered: set of all discovered tool names
        """
        if not self._mcp_manager:
            return {
                "server_to_tools": {},
                "tool_to_server": {},
                "allowed_tools": set(),
                "all_discovered": set(),
            }
        server_to_tools = self._mcp_manager.server_to_tools
        tool_to_server = self._mcp_manager.tool_to_server
        allowed = self._mcp_runtime.get_allowed_tools() if self._mcp_runtime else set()
        all_discovered = set(tool_to_server.keys())
        return {
            "server_to_tools": server_to_tools,
            "tool_to_server": tool_to_server,
            "allowed_tools": allowed,
            "all_discovered": all_discovered,
        }

    # -------------------------------------------------------------------------
    # Direct tool access
    # -------------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool directly without going through the LLM."""
        if not self._mcp_manager:
            raise RuntimeError("No MCP tools configured")
        return await self._mcp_manager.call_tool(
            tool_name=tool_name,
            arguments=arguments,
            timeout_s=self._mcp_runtime.tool_timeout_s if self._mcp_runtime else DEFAULT_TOOL_TIMEOUT,
        )
