# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Escalation logging and reading for critical pipeline issues."""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path


class EscalationLogger:
    """Appends critical escalation entries to a JSONL file.

    Uses file locking so multiple agents can safely write to the same log.
    """

    def __init__(self, escalations_path: Path) -> None:
        self._path = escalations_path
        self.task_id: str | None = None

    def log(self, severity: str, message: str, agent_id: str) -> None:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent_id": agent_id,
            "severity": severity,
            "message": message,
            "task_id": self.task_id,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


class EscalationReader:
    """Reads escalations from a JSONL file, filtered by agent IDs.

    Used by the trace analyzer to see escalations raised by workers
    that worked on a specific task.
    """

    def __init__(self, escalations_path: Path, traces_dir: Path, task_id: str) -> None:
        self._path = escalations_path
        self._traces_dir = traces_dir
        self._task_id = task_id

    def _task_agent_ids(self) -> set[str]:
        """Collect all agent IDs that worked on this task across all attempts."""
        import re

        task_dir = self._traces_dir / "tasks" / self._task_id
        if not task_dir.exists():
            return set()
        ids: set[str] = set()
        for d in task_dir.iterdir():
            if d.is_dir() and re.match(r"attempt_\d+$", d.name):
                for f in d.glob("*.json"):
                    if f.stem != "steps":
                        ids.add(f.stem)
        return ids

    def get_escalations(self) -> str:
        """Return all escalations from agents that worked on this task."""
        if not self._path.exists():
            return "No escalations found for this task."

        entries: list[dict] = []
        fallback_entries: list[dict] = []
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("task_id") == self._task_id:
                    entries.append(entry)
                elif entry.get("task_id") is None:
                    fallback_entries.append(entry)

        # Backward compat: entries without task_id fall back to agent-ID matching
        if fallback_entries:
            agent_ids = self._task_agent_ids()
            for entry in fallback_entries:
                if entry.get("agent_id") in agent_ids:
                    entries.append(entry)

        if not entries:
            return "No escalations found for this task."

        parts = [f"# Escalations for task: {self._task_id}", f"({len(entries)} found)", ""]
        for e in entries:
            parts.append(
                f"**[{e.get('severity', '?')}]** {e.get('agent_id', '?')} "
                f"({e.get('timestamp', '?')})\n{e.get('message', '')}\n"
            )
        return "\n".join(parts)
