# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Trace persistence — saves trace objects to JSON files.

Trace IDs may contain '/' to create subdirectories:
  "orchestrator"              → run_dir/orchestrator.json
  "convex-sets/attempt_1"    → run_dir/convex-sets/attempt_1.json
  "convex-sets/analyzer"     → run_dir/convex-sets/analyzer.json
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class TraceStore:
    """Saves and loads traces to a run directory.

    Each trace is saved as run_dir/{trace_id}.json where trace_id may
    contain '/' to place the file in a subdirectory.
    """

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def create_run(cls, base_dir: Path, run_name: str | None = None) -> TraceStore:
        """Create a TraceStore with a timestamped run folder."""
        if run_name is None:
            run_name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return cls(base_dir / run_name)

    def _trace_path(self, trace_id: str) -> Path:
        """Resolve trace_id to a file path, supporting subdirectories via '/'."""
        return self.run_dir / f"{trace_id}.json"

    def save(self, trace: Any) -> None:
        """Save a trace to its JSON file, creating subdirectories as needed.

        The trace must have a `trace_id` attribute and a `to_dict()` method.
        Uses write-to-temp-then-rename to avoid truncated files on crash.
        """
        trace_id = getattr(trace, "trace_id", None)
        output_path = self._trace_path(trace_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(trace.to_dict(), f, indent=2)
        tmp.rename(output_path)

    def load(self, trace_id: str) -> dict | None:
        """Load a specific trace by ID. Returns None if missing or corrupt."""
        path = self._trace_path(trace_id)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return None

    def load_all(self) -> list[dict]:
        """Load all traces from the run directory (recursive)."""
        if not self.run_dir.exists():
            return []
        traces = []
        for path in sorted(self.run_dir.rglob("*.json")):
            with open(path) as f:
                traces.append(json.load(f))
        return traces

    def list_traces(self) -> list[str]:
        """List all trace IDs in this run (recursive, relative to run_dir)."""
        if not self.run_dir.exists():
            return []
        return [str(p.relative_to(self.run_dir).with_suffix("")) for p in sorted(self.run_dir.rglob("*.json"))]

    def list_task_ids(self) -> list[str]:
        """List task IDs that have a subdirectory in this run."""
        if not self.run_dir.exists():
            return []
        return sorted(p.name for p in self.run_dir.iterdir() if p.is_dir())

    def clear(self) -> None:
        """Clear all traces from the run directory."""
        import shutil

        if self.run_dir.exists():
            for path in self.run_dir.rglob("*.json"):
                path.unlink()
            for path in self.run_dir.iterdir():
                if path.is_dir():
                    shutil.rmtree(path)
