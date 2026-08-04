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
import contextlib
import fcntl
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BUDGET_FILE = "budget.json"
RESERVATIONS_FILE = "spend_reservations.json"
RESERVATION_TTL_SECONDS = 6 * 3600
#: Spend classes that actually cost the operator something. A `subscription`
#: run still consumes a rate window, so it is paced too — only cost-free
#: bookkeeping would be exempt, and there is none.
_COUNTED_STATUSES = ("proved", "failed")


class BudgetConfigError(ValueError):
    """The repository opted into pacing but its budget file is invalid."""


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
    """The project's budget, or an unlimited one when no file exists.

    An existing malformed file raises: silently disabling an explicit spending
    control is more dangerous than leaving work queued until the file is fixed.
    """
    path = budget_path(project_dir)
    if not path.exists():
        return Budget()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BudgetConfigError(f"cannot parse {path}: {error}") from error
    if not isinstance(data, dict):
        raise BudgetConfigError(f"{path} must contain a JSON object")
    backends = data.get("backends")
    if backends is not None and not isinstance(backends, dict):
        raise BudgetConfigError(f"{path}: backends must be an object")
    normalized_backends = {}
    for name, limits in (backends or {}).items():
        if not isinstance(limits, dict):
            raise BudgetConfigError(f"{path}: backend {name!r} limits must be an object")
        normalized_backends[normalize_backend(str(name))] = {
            "max_runs": int(_budget_number(limits, "max_runs", path, integer=True)),
            "max_wall_seconds": _budget_number(limits, "max_wall_seconds", path),
        }
    return Budget(
        window_hours=_budget_number(data, "window_hours", path),
        max_runs=int(_budget_number(data, "max_runs", path, integer=True)),
        max_wall_seconds=_budget_number(data, "max_wall_seconds", path),
        backends=normalized_backends,
    )


def _budget_number(data: dict, key: str, path: Path, *, integer: bool = False) -> float:
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise BudgetConfigError(f"{path}: {key} must be a non-negative number")
    if integer and int(value) != value:
        raise BudgetConfigError(f"{path}: {key} must be an integer")
    return float(value)


def _number(value: Any) -> float:
    """A non-negative number, or 0.0 (= 'no limit') for anything unusable."""
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def normalize_backend(backend: str) -> str:
    """Map user-facing backend names to ledger adapter identifiers."""
    return {"max": "claude"}.get(backend, backend)


def _state_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".autoform"


@contextlib.contextmanager
def _reservation_lock(project_dir: str | Path):
    state = _state_dir(project_dir)
    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / "spend.lock"
    if lock_path.is_symlink():
        raise BudgetConfigError(f"spend lock must not be a symlink: {lock_path}")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield state
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_reservations(state: Path, now: float) -> list[dict]:
    path = state / RESERVATIONS_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)
            and isinstance(item.get("at"), (int, float))
            and now - float(item["at"]) < RESERVATION_TTL_SECONDS]


def _save_reservations(state: Path, reservations: list[dict]) -> None:
    path = state / RESERVATIONS_FILE
    if path.is_symlink():
        raise BudgetConfigError(f"reservation file must not be a symlink: {path}")
    fd, tmp = tempfile.mkstemp(dir=state, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(reservations, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _add_reservations(usage: dict, reservations: list[dict]) -> dict:
    usage = {"runs": usage["runs"], "wall_seconds": usage["wall_seconds"],
             "backends": {key: dict(value) for key, value in usage["backends"].items()}}
    for item in reservations:
        backend = normalize_backend(str(item.get("backend") or "unknown"))
        usage["runs"] += 1
        slot = usage["backends"].setdefault(backend, {"runs": 0, "wall_seconds": 0.0})
        slot["runs"] += 1
    return usage


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
        backend = normalize_backend(str(entry.get("backend") or "unknown"))
        runs += 1
        wall += seconds
        slot = per.setdefault(backend, {"runs": 0, "wall_seconds": 0.0})
        slot["runs"] += 1
        slot["wall_seconds"] += seconds
    return {"runs": runs, "wall_seconds": wall, "backends": per}


def _evaluate(budget: Budget, usage: dict, backend: str) -> dict:
    backend = normalize_backend(backend)
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


def check(project_dir: str | Path, backend: str, now: float | None = None) -> dict:
    """Whether an expensive run may start now.

    Returns ``{"allowed": bool, "reason": str, "usage": {...}, "budget": {...}}``.
    ``allowed`` is True whenever no budget is configured — pacing is opt-in.
    """
    import time

    now = time.time() if now is None else now
    try:
        budget = load_budget(project_dir)
    except BudgetConfigError as error:
        return {"allowed": False, "reason": str(error), "usage": {}, "paced": True,
                "configuration_error": True}
    if not budget.enabled:
        return {"allowed": True, "reason": "no budget configured", "usage": {}, "paced": False}
    with _reservation_lock(project_dir) as state:
        usage = window_usage(project_dir, budget.window_hours, now)
        usage = _add_reservations(usage, _load_reservations(state, now))
    return _evaluate(budget, usage, backend)


def reserve(project_dir: str | Path, backend: str, now: float | None = None) -> dict:
    """Atomically claim one run slot, returning the same decision shape as check()."""
    import time

    now = time.time() if now is None else now
    try:
        budget = load_budget(project_dir)
    except BudgetConfigError as error:
        return {"allowed": False, "reason": str(error), "usage": {}, "paced": True,
                "configuration_error": True, "reservation_id": None}
    if not budget.enabled:
        return {"allowed": True, "reason": "no budget configured", "usage": {},
                "paced": False, "reservation_id": None}
    with _reservation_lock(project_dir) as state:
        reservations = _load_reservations(state, now)
        usage = _add_reservations(
            window_usage(project_dir, budget.window_hours, now), reservations
        )
        result = _evaluate(budget, usage, backend)
        if not result["allowed"]:
            result["reservation_id"] = None
            return result
        reservation_id = uuid.uuid4().hex
        reservations.append({"id": reservation_id, "backend": normalize_backend(backend), "at": now})
        _save_reservations(state, reservations)
        result["reservation_id"] = reservation_id
        return result


def release(project_dir: str | Path, reservation_id: str | None) -> None:
    if not reservation_id:
        return
    import time

    with _reservation_lock(project_dir) as state:
        reservations = _load_reservations(state, time.time())
        _save_reservations(state, [item for item in reservations if item.get("id") != reservation_id])


def record_run(project_dir: str | Path, backend: str, status: str, wall_seconds: float,
               *, node: str = "") -> None:
    """Append a central usage entry for work executed in a disposable checkout."""
    state = _state_dir(project_dir)
    state.mkdir(parents=True, exist_ok=True)
    ledger = state / "usage.jsonl"
    if ledger.is_symlink():
        raise BudgetConfigError(f"usage ledger must not be a symlink: {ledger}")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "node": node,
        "backend": normalize_backend(backend),
        "status": status if status in _COUNTED_STATUSES else "failed",
        "wall_seconds": max(0.0, float(wall_seconds)),
    }
    encoded = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


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
