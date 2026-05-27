# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Trace inspection — targeted query tools over agent traces for one task.

Directory structure:
    traces/{task_id}/
        attempt_1/
            steps.json          ← step_trace_context (winner, status, build/review steps)
            worker-0.json       ← AgentTrace
            reviewer-0.json     ← AgentTrace
        attempt_2/
            ...
        analyzer.json

All tools default to the latest attempt. Pass attempt_number to override.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


class TraceInspector:
    """Targeted query tools over trace files for one task.

    Args:
        traces_dir: Root traces directory (contains per-task subdirectories).
        task_id: The task whose traces to inspect.
    """

    def __init__(self, traces_dir: Path | str, task_id: str) -> None:
        self.traces_dir = Path(traces_dir)
        self.task_id = task_id
        self._task_dir = self.traces_dir / "tasks" / task_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _attempt_numbers(self) -> list[int]:
        if not self._task_dir.exists():
            return []
        nums = []
        for p in self._task_dir.iterdir():
            if p.is_dir():
                m = re.match(r"attempt_(\d+)$", p.name)
                if m:
                    nums.append(int(m.group(1)))
        return sorted(nums)

    def _latest(self) -> int | None:
        nums = self._attempt_numbers()
        return nums[-1] if nums else None

    def _resolve(self, attempt_number: int | None) -> int | None:
        return attempt_number if attempt_number is not None else self._latest()

    def _attempt_dir(self, n: int) -> Path:
        return self._task_dir / f"attempt_{n}"

    def _load_steps(self, n: int) -> dict | None:
        path = self._attempt_dir(n) / "steps.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def _load_agent(self, n: int, agent_id: str) -> dict | None:
        path = self._attempt_dir(n) / f"{agent_id}.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def _agent_ids(self, n: int) -> list[str]:
        d = self._attempt_dir(n)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.json") if p.stem != "steps")

    def _err_attempt(self, n: int) -> str:
        available = self._attempt_numbers()
        return f"Error: attempt {n} not found. Available: {available}"

    def _err_agent(self, n: int, agent_id: str) -> str:
        return f"Error: agent '{agent_id}' not found in attempt {n}. Available: {self._agent_ids(n)}"

    # ------------------------------------------------------------------
    # Attempt overview
    # ------------------------------------------------------------------

    def list_attempts(self) -> str:
        """List all attempts for this task with status, winner, and step counts."""
        nums = self._attempt_numbers()
        if not nums:
            return json.dumps({"task_id": self.task_id, "attempts": []})
        result = []
        for n in nums:
            steps_data = self._load_steps(n)
            if steps_data is None:
                result.append({"attempt": n, "error": "steps.json missing"})
                continue
            steps = steps_data.get("steps", [])
            result.append(
                {
                    "attempt": n,
                    "final_status": steps_data.get("final_status", "unknown"),
                    "winner_id": steps_data.get("winner_id"),
                    "agents": self._agent_ids(n),
                    "build_attempts": sum(1 for s in steps if s.get("function", "").endswith(".build")),
                    "failed_builds": sum(
                        1 for s in steps if s.get("function", "").endswith(".build") and not s.get("success")
                    ),
                    "review_rejections": sum(
                        1
                        for s in steps
                        if s.get("function", "").endswith(".review")
                        and isinstance(s.get("result_summary"), (list, tuple))
                        and len(s.get("result_summary", [])) > 0
                        and s["result_summary"][0] is False
                    ),
                }
            )
        return json.dumps({"task_id": self.task_id, "attempts": result}, indent=2)

    def get_step_timeline(self, attempt_number: int | None = None) -> str:
        """Ordered build/rebase/merge/review steps for an attempt (default: latest)."""
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        data = self._load_steps(n)
        if data is None:
            return self._err_attempt(n)
        steps = data.get("steps", [])
        if not steps:
            return f"Attempt {n}: no steps recorded."
        lines = [f"Attempt {n} step timeline ({len(steps)} steps):"]
        for i, s in enumerate(steps):
            fname = s.get("function", "?").split(".")[-1]
            ok = "OK" if s.get("success") else "FAIL"
            dur = f"{s.get('duration_ms', 0):.0f}ms"
            agent = s.get("args_summary", {}).get("agent", "")
            agent_str = f" [{agent}]" if agent else ""
            lines.append(f"  [{i}] {fname}{agent_str} {ok} ({dur})")
        return "\n".join(lines)

    def get_build_errors(self, attempt_number: int | None = None) -> str:
        """All failed build steps with error text (default: latest attempt)."""
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        data = self._load_steps(n)
        if data is None:
            return self._err_attempt(n)
        failed = [s for s in data.get("steps", []) if s.get("function", "").endswith(".build") and not s.get("success")]
        if not failed:
            return f"Attempt {n}: no build errors."
        lines = [f"Attempt {n}: {len(failed)} build error(s):"]
        for s in failed:
            r = s.get("result_summary", [None, None])
            text = r[1] if isinstance(r, (list, tuple)) and len(r) > 1 else s.get("error", "no message")
            agent = s.get("args_summary", {}).get("agent", "?")
            lines.append(f"\n  Agent {agent}:\n{text}")
        return "\n".join(lines)

    def get_review_feedback(self, attempt_number: int | None = None) -> str:
        """All review rejections with feedback text (default: latest attempt)."""
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        data = self._load_steps(n)
        if data is None:
            return self._err_attempt(n)
        rejections = [
            s
            for s in data.get("steps", [])
            if s.get("function", "").endswith(".review")
            and isinstance(s.get("result_summary"), (list, tuple))
            and len(s.get("result_summary", [])) > 0
            and s["result_summary"][0] is False
        ]
        if not rejections:
            return f"Attempt {n}: no review rejections."
        lines = [f"Attempt {n}: {len(rejections)} rejection(s):"]
        for s in rejections:
            feedback = s["result_summary"][1] if len(s["result_summary"]) > 1 else "no feedback"
            agent = s.get("args_summary", {}).get("agent", "?")
            lines.append(f"\n  Agent {agent}:\n{feedback}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Agent navigation
    # ------------------------------------------------------------------

    def list_agents(self, attempt_number: int | None = None) -> str:
        """List agent IDs that ran in an attempt (default: latest)."""
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        ids = self._agent_ids(n)
        if not ids:
            return self._err_attempt(n)
        return json.dumps({"attempt": n, "agents": ids})

    def get_agent_stats(self, agent_id: str, attempt_number: int | None = None) -> str:
        """Summary stats for one agent: turns, tool counts, cost (default: latest attempt)."""
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        data = self._load_agent(n, agent_id)
        if data is None:
            return self._err_agent(n, agent_id)
        summary = data.get("summary", {})
        tool_calls = data.get("tool_calls", [])
        ok = sum(1 for tc in tool_calls if tc.get("success"))
        return json.dumps(
            {
                "attempt": n,
                "agent_id": agent_id,
                "final_status": data.get("final_status"),
                "total_turns": data.get("total_turns", 0),
                "total_tool_calls": len(tool_calls),
                "successful_tool_calls": ok,
                "failed_tool_calls": len(tool_calls) - ok,
                "total_tokens": summary.get("total_tokens", 0),
                "total_cost_usd": summary.get("total_cost_usd", 0.0),
            },
            indent=2,
        )

    def get_tool_stats(self, agent_id: str, attempt_number: int | None = None) -> str:
        """Per-tool breakdown: call count, success/fail, avg duration (default: latest attempt)."""
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        data = self._load_agent(n, agent_id)
        if data is None:
            return self._err_agent(n, agent_id)
        by_tool: dict[str, list] = {}
        for tc in data.get("tool_calls", []):
            name = tc.get("tool_name", "unknown")
            by_tool.setdefault(name, []).append(tc)
        rows = []
        for name, calls in sorted(by_tool.items()):
            ok = sum(1 for c in calls if c.get("success"))
            durations = [c.get("duration_ms") or 0 for c in calls]
            avg_ms = sum(durations) / len(durations) if durations else 0
            rows.append(
                {
                    "tool": name,
                    "count": len(calls),
                    "ok": ok,
                    "fail": len(calls) - ok,
                    "avg_ms": round(avg_ms),
                }
            )
        return json.dumps({"attempt": n, "agent_id": agent_id, "tools": rows}, indent=2)

    def get_failed_tools(self, agent_id: str, attempt_number: int | None = None) -> str:
        """All failed tool calls with error messages (default: latest attempt)."""
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        data = self._load_agent(n, agent_id)
        if data is None:
            return self._err_agent(n, agent_id)
        failed = [(i, tc) for i, tc in enumerate(data.get("tool_calls", [])) if not tc.get("success")]
        if not failed:
            return f"Agent {agent_id} attempt {n}: no failed tool calls."
        lines = [f"Agent {agent_id} attempt {n}: {len(failed)} failed call(s):"]
        for i, tc in failed:
            lines.append(f"\n  [{i}] {tc.get('tool_name', '?')}: {tc.get('error', 'no error message')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Message / tool drill-down
    # ------------------------------------------------------------------

    def get_messages(self, agent_id: str, last_n: int = 10, offset: int = 0, attempt_number: int | None = None) -> str:
        """Messages from the agent's conversation (default: latest attempt).

        Returns all roles (user, assistant, tool results) for full context.
        Use offset to paginate from the end (offset=0 is most recent).

        Args:
            agent_id: Agent to read.
            last_n: Number of messages to return.
            offset: Skip this many messages from the end before selecting.
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        data = self._load_agent(n, agent_id)
        if data is None:
            return self._err_agent(n, agent_id)
        messages = data.get("messages", [])
        non_system = [m for m in messages if m.get("role") != "system"]
        total = len(non_system)
        end = total - offset
        start = max(0, end - last_n)
        if end <= 0:
            return f"No messages at offset {offset} (total: {total})."
        selected = non_system[start:end]
        lines = [f"Agent {agent_id} attempt {n}: messages {start + 1}–{end} of {total}:"]
        for msg in selected:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                )
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                lines.append(f"\n[{role}] (calls: {', '.join(names)})")
                if content:
                    lines.append(content[:500] + ("..." if len(content) > 500 else ""))
            else:
                lines.append(f"\n[{role}]")
                lines.append(content[:1000] + ("..." if len(content) > 1000 else ""))
        return "\n".join(lines)

    def get_tool_call(self, agent_id: str, call_index: int, attempt_number: int | None = None) -> str:
        """Full arguments and result of one tool call by index (default: latest attempt).

        Use get_tool_stats to find call indexes.
        """
        n = self._resolve(attempt_number)
        if n is None:
            return "Error: no attempts found."
        data = self._load_agent(n, agent_id)
        if data is None:
            return self._err_agent(n, agent_id)
        tool_calls = data.get("tool_calls", [])
        if call_index < 0 or call_index >= len(tool_calls):
            return f"Error: call_index {call_index} out of range (0–{len(tool_calls) - 1})"
        tc = tool_calls[call_index]
        parts = [
            f"Tool: {tc.get('tool_name', '')}",
            f"Success: {tc.get('success', False)}",
            f"Duration: {tc.get('duration_ms', 0):.0f}ms",
            f"Arguments:\n{json.dumps(tc.get('arguments', {}), indent=2)}",
        ]
        if tc.get("error"):
            parts.append(f"Error:\n{tc['error']}")
        if tc.get("result"):
            parts.append(f"Result:\n{tc['result']}")
        return "\n".join(parts)
