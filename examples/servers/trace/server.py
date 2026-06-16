"""Trace MCP server — record and query formalization execution traces."""

from __future__ import annotations

import json
import os

from fastmcp.server import FastMCP

from .core import TraceStore


def create_trace_server(store: TraceStore) -> FastMCP:
    """Create a FastMCP server wrapping a TraceStore.

    Provides tools for both recording events (used by agents during
    formalization) and querying them (used for review and analysis).
    """
    server = FastMCP(name="autoform-trace")

    # --- Recording tools ---

    @server.tool
    def record_proof_attempt(
        theorem: str,
        status: str,
        lean_code: str = "",
        error: str = "",
        agent: str = "",
    ) -> str:
        """Record a proof attempt.

        Args:
            theorem: Name of the theorem being proved.
            status: "success", "failure", or "in_progress".
            lean_code: The Lean code attempted.
            error: Error message if failed.
            agent: Agent ID that made the attempt.
        """
        store.record(
            "proof_attempt",
            agent=agent,
            theorem=theorem,
            status=status,
            lean_code=lean_code,
            error=error,
        )
        return f"Recorded proof attempt for {theorem}: {status}"

    @server.tool
    def record_step(
        action: str,
        result: str = "",
        agent: str = "",
    ) -> str:
        """Record a general agent action step.

        Args:
            action: Description of what the agent did.
            result: Outcome or output.
            agent: Agent ID.
        """
        store.record("step", agent=agent, action=action, result=result)
        return "Step recorded."

    @server.tool
    def record_review(
        target: str,
        verdict: str,
        feedback: str = "",
        agent: str = "",
    ) -> str:
        """Record a review decision.

        Args:
            target: What was reviewed (file path or theorem name).
            verdict: "approved" or "rejected".
            feedback: Review feedback text.
            agent: Reviewer agent ID.
        """
        store.record(
            "review",
            agent=agent,
            target=target,
            verdict=verdict,
            feedback=feedback,
        )
        return f"Review recorded for {target}: {verdict}"

    # --- Query tools ---

    @server.tool
    def get_progress() -> str:
        """Get a summary of the current formalization run.

        Returns proof attempt counts, success/failure rates,
        and per-agent event counts.
        """
        summary = store.get_summary()
        return json.dumps(summary, indent=2)

    @server.tool
    def get_proof_attempts(
        last_n: int = 20,
    ) -> str:
        """Get recent proof attempts.

        Args:
            last_n: Number of recent attempts to return.
        """
        events = store.get_events(event_type="proof_attempt", last_n=last_n)
        if not events:
            return "No proof attempts recorded."
        return json.dumps(events, indent=2)

    @server.tool
    def get_reviews(
        last_n: int = 10,
    ) -> str:
        """Get recent review decisions.

        Args:
            last_n: Number of recent reviews to return.
        """
        events = store.get_events(event_type="review", last_n=last_n)
        if not events:
            return "No reviews recorded."
        return json.dumps(events, indent=2)

    @server.tool
    def list_runs() -> str:
        """List all available trace runs."""
        runs = store.list_runs()
        if not runs:
            return "No runs found."
        return json.dumps(runs)

    @server.tool
    def load_run(run_id: str) -> str:
        """Load a previous run's trace data for querying.

        Args:
            run_id: The run identifier to load.
        """
        try:
            store.load_run(run_id)
            summary = store.get_summary()
            return f"Loaded run '{run_id}': {summary['total_events']} events, {summary['proof_attempts']} proof attempts"
        except FileNotFoundError:
            return f"Run '{run_id}' not found. Use list_runs to see available runs."

    return server


if __name__ == "__main__":
    trace_dir = os.environ.get("AUTOFORM_TRACE_DIR", "./traces")
    store = TraceStore(trace_dir)

    run_id = os.environ.get("AUTOFORM_RUN_ID", "default")
    store.start_run(run_id)

    server = create_trace_server(store)
    server.run(transport="stdio")
