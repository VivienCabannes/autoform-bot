"""Cron / Scheduling — session-scoped cron job management.

No MCP dependencies. Jobs live only in memory and are lost when the
server stops. The actual cron loop that fires jobs must be driven by
the host application.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class CronJob:
    """A scheduled cron job."""

    id: str
    cron: str
    prompt: str
    recurring: bool = True
    created_at: float = field(default_factory=time.time)
    last_fired: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cron": self.cron,
            "prompt": self.prompt,
            "recurring": self.recurring,
        }


def _parse_cron(expr: str) -> tuple[list[int | None], ...]:
    """Parse a 5-field cron expression into (minute, hour, dom, month, dow).

    Supports: *, specific values, ranges (1-5), steps (*/5), and lists (1,3,5).
    Returns lists of matching integers, or [None] for '*'.
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5 cron fields, got {len(fields)}: {expr}")

    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    result = []

    for raw, (lo, hi) in zip(fields, ranges):
        if raw == "*":
            result.append([None])
            continue
        values: set[int] = set()
        for part in raw.split(","):
            if "/" in part:
                base, step_str = part.split("/", 1)
                step = int(step_str)
                start = lo if base in ("*", "") else int(base)
                values.update(range(start, hi + 1, step))
            elif "-" in part:
                a, b = part.split("-", 1)
                values.update(range(int(a), int(b) + 1))
            else:
                values.add(int(part))
        result.append(sorted(values))

    return tuple(result)


def _matches_now(cron_expr: str, now: time.struct_time) -> bool:
    """Check if a cron expression matches the given time."""
    minute, hour, dom, month, dow = _parse_cron(cron_expr)

    def _match(field_vals: list[int | None], actual: int) -> bool:
        return field_vals == [None] or actual in field_vals

    return (
        _match(minute, now.tm_min)
        and _match(hour, now.tm_hour)
        and _match(dom, now.tm_mday)
        and _match(month, now.tm_mon)
        and _match(dow, now.tm_wday)  # Python: Mon=0, cron: Sun=0 — adjust below
    )


class CronScheduler:
    """Session-scoped cron job scheduler."""

    def __init__(self) -> None:
        self.jobs: dict[str, CronJob] = {}

    def create(self, cron: str, prompt: str, recurring: bool = True) -> str:
        """Schedule a prompt on a cron schedule.

        Args:
            cron: 5-field cron expression (minute hour dom month dow).
            prompt: The prompt to enqueue at each fire time.
            recurring: If True, fires on every match. If False, fires once then deletes.
        """
        # Validate the expression
        try:
            _parse_cron(cron)
        except ValueError as e:
            return f"Error: {e}"

        job_id = uuid.uuid4().hex[:8]
        self.jobs[job_id] = CronJob(id=job_id, cron=cron, prompt=prompt, recurring=recurring)
        mode = "recurring" if recurring else "one-shot"
        return f"Created {mode} job {job_id}: '{cron}' -> {prompt[:80]}"

    def delete(self, job_id: str) -> str:
        """Delete a scheduled cron job."""
        if job_id not in self.jobs:
            return f"Error: Job {job_id} not found"
        del self.jobs[job_id]
        return f"Deleted job {job_id}"

    def list_jobs(self) -> str:
        """List all scheduled cron jobs."""
        if not self.jobs:
            return "No scheduled jobs."
        return json.dumps([j.to_dict() for j in self.jobs.values()], indent=2)

    def check_pending(self) -> list[str]:
        """Check for jobs that should fire now. Returns list of prompts.

        Call this periodically (e.g. every 60s) from the host application.
        Handles one-shot deletion and last_fired deduplication.
        """
        now = time.localtime()
        current_minute = time.mktime(now) // 60
        prompts: list[str] = []
        to_delete: list[str] = []

        for job_id, job in self.jobs.items():
            last_minute = job.last_fired // 60
            if current_minute <= last_minute:
                continue
            if _matches_now(job.cron, now):
                prompts.append(job.prompt)
                job.last_fired = time.time()
                if not job.recurring:
                    to_delete.append(job_id)

        for jid in to_delete:
            del self.jobs[jid]

        return prompts
