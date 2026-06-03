"""Trace recording and querying — structured JSONL tracing for formalization runs.

Records proof attempts, agent actions, and step-level events. Emits
JSONL that a viewer can consume for live or post-hoc inspection.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

logger = getLogger(__name__)


@dataclass
class TraceEvent:
    """One trace event."""

    timestamp: float
    event_type: str
    agent: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "agent": self.agent,
            **self.data,
        }


class TraceStore:
    """Append-only JSONL trace store.

    Each trace file is one formalization session/run. Events are
    appended as JSONL lines for streaming reads.
    """

    def __init__(self, trace_dir: str | Path) -> None:
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._current_file: Path | None = None
        self._events: list[TraceEvent] = []

    def start_run(self, run_id: str) -> None:
        """Start a new trace file for a run."""
        self._current_file = self.trace_dir / f"{run_id}.jsonl"
        self._events = []

    def record(
        self,
        event_type: str,
        agent: str = "",
        **data: Any,
    ) -> None:
        """Record a trace event."""
        event = TraceEvent(
            timestamp=time.time(),
            event_type=event_type,
            agent=agent,
            data=data,
        )
        self._events.append(event)

        if self._current_file:
            with open(self._current_file, "a") as f:
                f.write(json.dumps(event.to_dict()) + "\n")

    def get_events(
        self,
        event_type: str | None = None,
        agent: str | None = None,
        last_n: int | None = None,
    ) -> list[dict]:
        """Query events with optional filtering."""
        events = self._events
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if agent:
            events = [e for e in events if e.agent == agent]
        if last_n:
            events = events[-last_n:]
        return [e.to_dict() for e in events]

    def get_summary(self) -> dict:
        """Return a summary of the current trace."""
        by_type: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        for e in self._events:
            by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
            if e.agent:
                by_agent[e.agent] = by_agent.get(e.agent, 0) + 1

        proof_attempts = [e for e in self._events if e.event_type == "proof_attempt"]
        succeeded = sum(1 for e in proof_attempts if e.data.get("status") == "success")
        failed = sum(1 for e in proof_attempts if e.data.get("status") == "failure")

        return {
            "total_events": len(self._events),
            "events_by_type": by_type,
            "events_by_agent": by_agent,
            "proof_attempts": len(proof_attempts),
            "proofs_succeeded": succeeded,
            "proofs_failed": failed,
        }

    def load_run(self, run_id: str) -> None:
        """Load events from an existing trace file."""
        path = self.trace_dir / f"{run_id}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"No trace file for run: {run_id}")

        self._current_file = path
        self._events = []

        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                self._events.append(TraceEvent(
                    timestamp=data.get("timestamp", 0),
                    event_type=data.get("event_type", "unknown"),
                    agent=data.get("agent", ""),
                    data={k: v for k, v in data.items() if k not in ("timestamp", "event_type", "agent")},
                ))

    def list_runs(self) -> list[str]:
        """List available run IDs."""
        return sorted(p.stem for p in self.trace_dir.glob("*.jsonl"))
