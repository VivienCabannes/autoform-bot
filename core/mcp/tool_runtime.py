# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP Tool Runtime — routes tool calls to MCP servers with filtering and tracing."""

from __future__ import annotations

import json
import logging
import math
import re
import time
import traceback
from pathlib import Path
from typing import Any

from .manager import MCPClientManager
from .bridge import (
    mcp_call_result_to_tool_content,
    mcp_tools_to_schemas,
    parse_tool_call_arguments,
)

from ..inference import ToolCall, ToolResult, ToolSchema
from ..tool import ToolSpec
from ..trace import AgentTrace, ToolCallRecord

logger = logging.getLogger(__name__)

_MAX_TRACE_RESULT_CHARS = 4000  # Truncation limit for trace records (not LLM output)

# --- Tool result budgeting constants ---
SYSTEM_MAX_RESULT_CHARS = 50_000  # Absolute cap per tool (unless inf)
MAX_RESULTS_PER_MESSAGE_CHARS = 200_000  # Per-message aggregate budget
PREVIEW_SIZE_CHARS = 2_000  # Preview length when result is persisted
PERSISTENCE_MARKER = "[Result persisted to"  # Marker to detect already-persisted results


class MCPToolRuntime:
    """Routes tool calls to MCP servers with optional tool filtering and tracing.

    When ``persist_dir`` is set, large tool results are written to disk and
    replaced with a preview instead of being hard-truncated. Two budget
    levels are enforced:

    1. **Per-tool**: each tool declares ``max_result_chars`` on its ``ToolSpec``
       (default 20K), capped at ``SYSTEM_MAX_RESULT_CHARS`` (50K). Tools with
       ``float('inf')`` (e.g. file reads) are exempt.
    2. **Per-message aggregate**: if the combined results from one batch of
       parallel tool calls exceeds ``MAX_RESULTS_PER_MESSAGE_CHARS`` (200K),
       the largest eligible results are persisted until under budget.

    When ``persist_dir`` is ``None``, results are hard-truncated (backward
    compatible with the previous behavior).
    """

    def __init__(
        self,
        *,
        manager: MCPClientManager,
        allowed_tools: list[str] | None = None,  # None treated as [] (all tools)
        tool_timeout_s: float | None = None,
        trace: AgentTrace | None = None,
        persist_dir: Path | None = None,
    ) -> None:
        self._manager = manager
        self._tool_timeout_s = tool_timeout_s
        self._allowed_tools = set(allowed_tools) if allowed_tools else set()
        self._trace = trace
        self._exchange_id: str | None = None
        self._persist_dir = persist_dir

    def set_allowed_tools(self, tools: list[str]) -> None:
        """Update the allowed tools allowlist at runtime."""
        self._allowed_tools = set(tools)

    def set_trace(self, trace: AgentTrace | None) -> None:
        """Set the trace for recording tool calls."""
        self._trace = trace

    def set_exchange_id(self, exchange_id: str | None) -> None:
        """Set the current exchange ID for tagging tool call records."""
        self._exchange_id = exchange_id

    @property
    def tool_timeout_s(self) -> float | None:
        """Timeout passed to the MCP manager for each tool call."""
        return self._tool_timeout_s

    def get_allowed_tools(self) -> set[str]:
        discovered = set(self._manager.tool_to_server)
        if not self._allowed_tools:
            return discovered
        return self._allowed_tools & discovered

    async def list_tools(self) -> list[ToolSchema]:
        """Return ToolSchema objects for allowed tools."""
        allowed = self.get_allowed_tools()
        tools = self._manager._get_discovered_tools(tool_names=allowed)
        return mcp_tools_to_schemas(tools)

    def apply_aggregate_budget(self, results: list[ToolResult]) -> None:
        """Enforce per-message aggregate budget on externally-assembled results.

        Use this when tool calls were executed individually (e.g. with
        per-tool semaphores) and their results need aggregate budgeting
        after reassembly.
        """
        self._apply_aggregate_budget(results)

    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """Execute tool calls and return ToolResult objects."""
        results: list[ToolResult] = []
        allowed = self.get_allowed_tools()

        for idx, tool_call in enumerate(tool_calls):
            call_id = tool_call.id or f"tool_call_{idx}"
            tool_name = tool_call.name

            if not tool_name:
                results.append(
                    ToolResult(
                        tool_call_id=call_id,
                        content=json.dumps(
                            {"ok": False, "error": "Malformed tool call: missing function name."}, ensure_ascii=False
                        ),
                        tool_name="",
                        is_error=True,
                    )
                )
                continue

            arguments = parse_tool_call_arguments(tool_call.arguments)

            if tool_name not in allowed:
                content = self._error_content(
                    tool_name=tool_name,
                    error=f"Tool '{tool_name}' is not allowed.",
                    arguments=arguments,
                )
                results.append(
                    ToolResult(
                        tool_call_id=call_id,
                        content=content,
                        tool_name=tool_name,
                        is_error=True,
                    )
                )
                continue

            record = ToolCallRecord(
                tool_name=tool_name,
                call_id=call_id,
                start_time=time.perf_counter(),
                exchange_id=self._exchange_id,
                arguments=arguments,
            )

            try:
                result = await self._manager.call_tool(
                    tool_name=tool_name,
                    arguments=arguments,
                    timeout_s=self._tool_timeout_s,
                )
                content = mcp_call_result_to_tool_content(result)
                record.complete(success=True)
                is_error = bool(getattr(result, "isError", False))
            except Exception as exc:
                traceback.print_exc()
                content = self._error_content(tool_name=tool_name, error=str(exc), arguments=arguments)
                record.complete(success=False, error=str(exc))
                is_error = True

            if self._trace:
                current_ex = self._trace.current_exchange()
                if current_ex and current_ex.exchange_id == self._exchange_id:
                    current_ex.tool_calls.append(record)
                else:
                    logger.warning("Tool call not recorded — no matching exchange: %s", record)
            agent_id = self._trace.id if self._trace else "?"
            logger.info("  [%s] %s", agent_id, record)

            # --- Empty result handling ---
            if not is_error and not content.strip():
                content = f"({tool_name} completed with no output)"

            # --- Per-tool budgeting ---
            limit = self._effective_limit(tool_name)
            if math.isfinite(limit) and len(content) > limit:
                if self._persist_dir:
                    content = self._persist_result(call_id, content)
                else:
                    content = (
                        content[: int(limit)]
                        + f"\n\n[truncated — {len(content):,} chars total, showing first {int(limit):,}]"
                    )

            # Update trace record with post-processed content
            trace_result = content[:_MAX_TRACE_RESULT_CHARS] if len(content) > _MAX_TRACE_RESULT_CHARS else content
            record.result = trace_result

            results.append(
                ToolResult(
                    tool_call_id=call_id,
                    content=content,
                    tool_name=tool_name,
                    is_error=is_error,
                )
            )

        # --- Per-message aggregate budget ---
        self._apply_aggregate_budget(results)

        return results

    # -------------------------------------------------------------------------
    # Tool result budgeting helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _effective_limit(tool_name: str) -> float:
        """Compute the effective per-tool char limit.

        Returns ``min(spec.max_result_chars, SYSTEM_MAX_RESULT_CHARS)`` for
        finite limits, or the raw value (inf) for tools that opt out of
        persistence.
        """
        raw = ToolSpec.max_result_chars_of(tool_name)
        if not math.isfinite(raw):
            return raw
        return min(raw, SYSTEM_MAX_RESULT_CHARS)

    def _persist_result(self, call_id: str, content: str) -> str:
        """Write full result to disk and return a preview string for the LLM."""
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", call_id)
        try:
            result_dir = self._persist_dir / "tool-results"
            result_dir.mkdir(parents=True, exist_ok=True)
            result_path = result_dir / f"{safe_id}.txt"
            result_path.write_text(content, encoding="utf-8")
        except OSError:
            logger.warning("Failed to persist tool result for %s; falling back to truncation", call_id, exc_info=True)
            return (
                content[:PREVIEW_SIZE_CHARS]
                + f"\n\n[truncated — {len(content):,} chars total, showing first {PREVIEW_SIZE_CHARS:,}]"
            )

        # Generate preview, preferring a newline boundary
        preview = content[:PREVIEW_SIZE_CHARS]
        last_nl = preview.rfind("\n")
        if last_nl > PREVIEW_SIZE_CHARS * 0.5:
            preview = content[:last_nl]

        has_more = len(content) > PREVIEW_SIZE_CHARS
        ellipsis = "...\n" if has_more else ""
        return (
            f"{preview}\n{ellipsis}\n"
            f"{PERSISTENCE_MARKER} {result_path} — {len(content):,} chars total, "
            f"showing first {PREVIEW_SIZE_CHARS:,}]"
        )

    def _apply_aggregate_budget(self, results: list[ToolResult]) -> None:
        """Persist largest results until total content fits within aggregate budget.

        Skips results that were already persisted (by per-tool limit),
        results from tools with infinite max_result_chars, and error results.
        Only applies when persist_dir is set.

        Note: mutates ``results`` in place (replaces elements at specific indices).
        """
        if not self._persist_dir:
            return

        total = sum(len(r.content) for r in results)
        if total <= MAX_RESULTS_PER_MESSAGE_CHARS:
            return

        # Build candidates: (index, content_len) for eligible results
        candidates: list[tuple[int, int]] = []
        for i, r in enumerate(results):
            if PERSISTENCE_MARKER in r.content:
                continue
            if not math.isfinite(self._effective_limit(r.tool_name)):
                continue
            if r.is_error:
                continue
            candidates.append((i, len(r.content)))

        # Persist largest first
        candidates.sort(key=lambda x: x[1], reverse=True)

        for idx, content_len in candidates:
            if total <= MAX_RESULTS_PER_MESSAGE_CHARS:
                break
            r = results[idx]
            old_len = len(r.content)
            persisted_content = self._persist_result(r.tool_call_id, r.content)
            results[idx] = ToolResult(
                tool_call_id=r.tool_call_id,
                content=persisted_content,
                tool_name=r.tool_name,
                is_error=r.is_error,
            )
            total = total - old_len + len(persisted_content)

    # -------------------------------------------------------------------------

    @staticmethod
    def _error_content(*, tool_name: str, error: str, arguments: dict[str, Any]) -> str:
        return json.dumps(
            {"ok": False, "tool": tool_name, "error": error, "arguments": arguments},
            ensure_ascii=False,
        )
