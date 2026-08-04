#!/usr/bin/env python3
"""Deterministic spend pacing for unattended runs — the autonomy safety valve.

An autonomous fleet has nobody watching the meter. Proof recovery is
deliberately uncapped in *attempts* (retries are gated by evidence, not by an
arbitrary count), which is right for correctness but means a hard node can
consume a subscription window without any human present to notice.

This governor bounds *resource use*, never judgment. It never asks a human
anything, never parks work permanently, and never decides a theorem is
hopeless: it just declines to start expensive work when the window's budget is
spent, and lets the fleet resume by itself when the window rolls. A paced fleet
is still fully autonomous — it is simply solvent.

Deliberately built on the project's own append-only prover ledger
(``.autoform/usage.jsonl``, written by every prove run through
``formalization.record_run``) rather than provider usage endpoints: no
credentials, no provider-specific scraping, identical behavior for every
backend, and trivially testable.

Budgets live in ``.autoform/budget.json`` (absent = unlimited)::

    {"window_hours": 5, "max_runs": 40, "max_wall_seconds": 14400,
     "backends": {"aristotle": {"max_runs": 10}}}

``window_hours`` is a rolling lookback, not a calendar window, so a fleet that
exhausts its budget recovers gradually rather than stampeding at a reset.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BUDGET_FILE = "budget.json"
#: Spend classes that actually cost the operator something. A `subscription`
#: run still consumes a rate window, so it is paced too — only cost-free
#: bookkeeping would be exempt, and there is none.
_COUNTED_STATUSES = ("proved", "failed")


@dataclass
class Budget:
    window_hours: float = 0.0
    max_runs: int = 0
    max_wall_seconds: float = 0.0
    backends: dict = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.window_hours and (self.max_runs or self.max_wall_seconds
                                           or self.backends))


def budget_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".autoform" / DEFAULT_BUDGET_FILE


def load_budget(project_dir: str | Path) -> Budget:
    """The project's budget, or an unlimited one. A malformed budget is treated
    as unlimited *and* reported by the caller — failing closed here would stall
    an unattended fleet on a typo, which is worse than an unpaced run."""
    path = budget_path(project_dir)
    if not path.exists():
        return Budget()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return Budget()
    if not isinstance(data, dict):
        return Budget()
    backends = data.get("backends")
    # Per-field coercion must fail open too: a typo ("5h", "forty") is a
    # configuration mistake, and stalling an unattended fleet over one is worse
    # than running that dimension unpaced.
    return Budget(
        window_hours=_number(data.get("window_hours")),
        max_runs=int(_number(data.get("max_runs"))),
        max_wall_seconds=_number(data.get("max_wall_seconds")),
        backends=backends if isinstance(backends, dict) else {},
    )


def _number(value: Any) -> float:
    """A non-negative number, or 0.0 (= 'no limit') for anything unusable."""
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _parse_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def window_usage(project_dir: str | Path, window_hours: float, now: float) -> dict:
    """Runs and wall-seconds inside the rolling window, overall and per backend."""
    ledger = Path(project_dir) / ".autoform" / "usage.jsonl"
    cutoff = now - window_hours * 3600.0
    runs = 0
    wall = 0.0
    per: dict[str, dict] = {}
    if not ledger.exists():
        return {"runs": 0, "wall_seconds": 0.0, "backends": {}}
    for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue          # a torn trailing line must never break pacing
        if not isinstance(entry, dict):
            continue
        stamp = _parse_ts(entry.get("ts") or entry.get("at"))
        if stamp is None or stamp < cutoff:
            continue
        if entry.get("status") not in _COUNTED_STATUSES:
            continue
        seconds = entry.get("wall_seconds")
        seconds = float(seconds) if isinstance(seconds, (int, float)) else 0.0
        backend = str(entry.get("backend") or "unknown")
        runs += 1
        wall += seconds
        slot = per.setdefault(backend, {"runs": 0, "wall_seconds": 0.0})
        slot["runs"] += 1
        slot["wall_seconds"] += seconds
    return {"runs": runs, "wall_seconds": wall, "backends": per}


def check(project_dir: str | Path, backend: str, now: float | None = None) -> dict:
    """Whether an expensive run may start now.

    Returns ``{"allowed": bool, "reason": str, "usage": {...}, "budget": {...}}``.
    ``allowed`` is True whenever no budget is configured — pacing is opt-in.
    """
    import time

    now = time.time() if now is None else now
    budget = load_budget(project_dir)
    if not budget.enabled:
        return {"allowed": True, "reason": "no budget configured", "usage": {}, "paced": False}
    usage = window_usage(project_dir, budget.window_hours, now)
    window = f"{budget.window_hours:g}h window"

    if budget.max_runs and usage["runs"] >= budget.max_runs:
        return {"allowed": False, "paced": True, "usage": usage,
                "reason": f"{usage['runs']}/{budget.max_runs} prover runs used in the {window}"}
    if budget.max_wall_seconds and usage["wall_seconds"] >= budget.max_wall_seconds:
        return {"allowed": False, "paced": True, "usage": usage,
                "reason": (f"{usage['wall_seconds']:.0f}/{budget.max_wall_seconds:.0f} prover "
                           f"seconds used in the {window}")}
    limits = budget.backends.get(backend)
    limits = limits if isinstance(limits, dict) else {}
    used = usage["backends"].get(backend) or {"runs": 0, "wall_seconds": 0.0}
    cap_runs = int(_number(limits.get("max_runs")))
    cap_wall = _number(limits.get("max_wall_seconds"))
    if cap_runs and used["runs"] >= cap_runs:
        return {"allowed": False, "paced": True, "usage": usage,
                "reason": f"backend {backend}: {used['runs']}/{cap_runs} runs used in the {window}"}
    if cap_wall and used["wall_seconds"] >= cap_wall:
        return {"allowed": False, "paced": True, "usage": usage,
                "reason": (f"backend {backend}: {used['wall_seconds']:.0f}/{cap_wall:.0f} "
                           f"seconds used in the {window}")}
    return {"allowed": True, "paced": True, "usage": usage,
            "reason": f"within budget ({usage['runs']} runs in the {window})"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check the project's prover spend budget.")
    ap.add_argument("project", type=Path)
    ap.add_argument("--backend", default="claude")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = check(args.project, args.backend)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(("ALLOW " if result["allowed"] else "PACE  ") + result["reason"])
    return 0 if result["allowed"] else 75      # 75 = the loop's no-progress code


if __name__ == "__main__":
    sys.exit(main())
