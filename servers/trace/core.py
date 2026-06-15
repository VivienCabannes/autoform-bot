"""Trace recording and querying — structured JSONL tracing for formalization runs.

Stub module. See examples/servers/trace/core.py for a full implementation
with append-only JSONL storage, per-run files, and summary aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceEvent:
    """One trace event."""

    timestamp: float
    event_type: str
    agent: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Flatten event to a plain dict for JSONL serialization."""
        raise NotImplementedError("See examples/servers/trace/core.py for reference implementation.")


class TraceStore:
    """Append-only JSONL trace store.

    Each trace file is one formalization session/run. Events are
    appended as JSONL lines for streaming reads.
    """

    def __init__(self, trace_dir: str) -> None:
        """Initialize the trace store.

        Args:
            trace_dir: Directory for storing JSONL trace files.
        """
        raise NotImplementedError("See examples/servers/trace/core.py for reference implementation.")

    def start_run(self, run_id: str) -> None:
        """Start a new trace file for a run.

        Args:
            run_id: Unique identifier for the run.
        """
        raise NotImplementedError("See examples/servers/trace/core.py for reference implementation.")

    def record(self, event_type: str, agent: str = "", **data: Any) -> None:
        """Record a trace event.

        Args:
            event_type: Category of event (e.g. "proof_attempt", "step", "review").
            agent: Agent ID that generated the event.
            **data: Event-specific payload fields.
        """
        raise NotImplementedError("See examples/servers/trace/core.py for reference implementation.")

    def get_events(
        self,
        event_type: str | None = None,
        agent: str | None = None,
        last_n: int | None = None,
    ) -> list[dict]:
        """Query events with optional filtering.

        Args:
            event_type: Filter by event type.
            agent: Filter by agent ID.
            last_n: Return only the last N events.

        Returns:
            List of event dicts.
        """
        raise NotImplementedError("See examples/servers/trace/core.py for reference implementation.")

    def get_summary(self) -> dict:
        """Return a summary of the current trace.

        Returns:
            Dict with total_events, events_by_type, events_by_agent,
            proof_attempts, proofs_succeeded, proofs_failed.
        """
        raise NotImplementedError("See examples/servers/trace/core.py for reference implementation.")

    def load_run(self, run_id: str) -> None:
        """Load events from an existing trace file.

        Args:
            run_id: The run identifier to load.
        """
        raise NotImplementedError("See examples/servers/trace/core.py for reference implementation.")

    def list_runs(self) -> list[str]:
        """List available run IDs.

        Returns:
            Sorted list of run ID strings.
        """
        raise NotImplementedError("See examples/servers/trace/core.py for reference implementation.")
