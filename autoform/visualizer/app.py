# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Autoform Visualizer — pipeline trace viewer.

Run with:
    python -m autoform.visualizer.app --runs-dir /path/to/runs

Then open: http://localhost:8001

Trace structure expected:
    {run_dir}/
        dag.json
        traces/
            orchestrator.json
            {task_id}/
                attempt_1/
                    steps.json
                    worker-0.json
                    reviewer-0.json
                attempt_2/
                    ...
                analyzer.json
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time as _time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from autoform.visualizer.trace_viewer import generate_html

WEBAPP_DIR = Path(__file__).parent
TEMPLATES_DIR = WEBAPP_DIR / "templates"

logger = logging.getLogger(__name__)

app = FastAPI(title="Autoform Visualizer V1")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _read_lib_name(code_root: Path) -> str | None:
    """Read the ``[[lean_lib]]`` name from a workspace lakefile."""
    lakefile = code_root / "lakefile.toml"
    if not lakefile.exists():
        return None
    from core.compat import tomllib

    with open(lakefile, "rb") as f:
        cfg = tomllib.load(f)
    libs = cfg.get("lean_lib", [])
    return libs[0]["name"] if libs else None


def get_runs_dir() -> Path:
    override = os.environ.get("VIZV1_RUNS_DIR")
    if override:
        return Path(override)
    return Path.cwd()


def _get_run_filter() -> str | None:
    """When set, only this run name is visible (used by hub gateway)."""
    return os.environ.get("VIZV1_RUN_FILTER") or None


def _get_registry_url() -> str | None:
    return os.environ.get("VIZV1_REGISTRY_URL") or None


def _load_urls_json(run_name: str) -> dict:
    """Load the consolidated urls.json for a run."""
    path = get_runs_dir() / run_name / "urls.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _discover_registry_urls(run_name: str) -> dict[int, str]:
    """Discover all registry URLs for a run.

    Reads from urls.json first, falls back to legacy registry_rank*.url files.
    Returns a dict mapping rank number to registry URL.
    """
    # Try consolidated urls.json first
    urls_data = _load_urls_json(run_name)
    if "registry" in urls_data:
        return {int(k): v for k, v in urls_data["registry"].items()}

    # Legacy: individual registry_rank*.url files
    run_dir = get_runs_dir() / run_name
    urls: dict[int, str] = {}
    for f in sorted(run_dir.glob("registry_rank*.url")):
        url = f.read_text().strip()
        if not url:
            continue
        rank_match = re.match(r"registry_rank(\d+)\.url", f.name)
        if rank_match:
            urls[int(rank_match.group(1))] = url
    if not urls:
        # Fall back to single registry.url or --registry CLI arg
        legacy = run_dir / "registry.url"
        if legacy.exists():
            url = legacy.read_text().strip()
            if url:
                urls[0] = url
        elif _get_registry_url():
            urls[0] = _get_registry_url()
    return urls


def _discover_control_urls(run_name: str) -> dict[int, str]:
    """Discover all control plane URLs for a run.

    Reads from urls.json first, falls back to legacy control.url file.
    """
    urls_data = _load_urls_json(run_name)
    if "control" in urls_data:
        return {int(k): v for k, v in urls_data["control"].items()}

    # Legacy: single control.url
    run_dir = get_runs_dir() / run_name
    legacy = run_dir / "control.url"
    if legacy.exists():
        url = legacy.read_text().strip()
        if url:
            return {0: url}
    return {}


def _route_agent_to_registry(agent_id: str, registry_urls: dict[int, str]) -> str | None:
    """Pick the registry URL for an agent based on its rank prefix.

    Agent IDs follow the pattern rank{N}-worker-{i} or rank{N}-reviewer-{i}.
    Registry URLs are keyed by rank number from registry_rank{N}.url files.
    The orchestrator and trace analyzers live on rank 0.
    """
    if not registry_urls:
        return None
    # Extract rank from agent_id (e.g. "rank2-worker-0" → "2")
    match = re.match(r"rank(\d+)-", agent_id)
    if match:
        rank = int(match.group(1))
        if rank in registry_urls:
            return registry_urls[rank]
        # rank not found — likely the coordinator's local worker (rank=world_size),
        # which runs on the same node as rank 0
        return registry_urls.get(0)
    # Non-rank-prefixed agents (orchestrator, trace_analyzer-*) are on rank 0
    return registry_urls.get(0)


# ── Formatting filters ────────────────────────────────────────────


def fmt_cost(cost: float | None) -> str:
    if not cost:
        return "$0.00"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_duration(s: float | None) -> str:
    if not s:
        return "—"
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    return f"{m}m {s % 60:.0f}s"


templates.env.filters["fmt_cost"] = fmt_cost
templates.env.filters["fmt_tokens"] = fmt_tokens
templates.env.filters["fmt_duration"] = fmt_duration


# ── Parallelism & Caching ─────────────────────────────────────────

_POOL = ThreadPoolExecutor(max_workers=16)

_JSON_CACHE: dict[Path, tuple[float, float, dict | None]] = {}  # path -> (cached_at, mtime, data)
_JSON_CACHE_TTL = 60.0  # seconds
_JSON_CACHE_MAX = 5000

_FN_CACHE: dict[str, tuple[float, Any]] = {}  # key -> (cached_at, result)
_FN_CACHE_TTL = 600.0
_FN_CACHE_MAX = 500


def _evict_cache(cache: dict, max_size: int) -> None:
    """Evict oldest entries when cache exceeds max_size."""
    if len(cache) <= max_size:
        return
    items = sorted(cache.items(), key=lambda kv: kv[1][0])
    to_remove = len(cache) - max_size
    for k, _ in items[:to_remove]:
        cache.pop(k, None)


def _cached(key: str, fn: Any, ttl: float = _FN_CACHE_TTL) -> Any:
    """Return cached result of *fn* if still fresh, otherwise recompute."""
    now = _time.monotonic()
    cached = _FN_CACHE.get(key)
    if cached and (now - cached[0]) < ttl:
        return cached[1]
    result = fn()
    _FN_CACHE[key] = (now, result)
    _evict_cache(_FN_CACHE, _FN_CACHE_MAX)
    return result


# ── Data loaders ──────────────────────────────────────────────────


def _load_json(path: Path) -> dict | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    now = _time.monotonic()
    cached = _JSON_CACHE.get(path)
    if cached and cached[1] == mtime and (now - cached[0]) < _JSON_CACHE_TTL:
        return cached[2]
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = None
    _JSON_CACHE[path] = (now, mtime, data)
    _evict_cache(_JSON_CACHE, _JSON_CACHE_MAX)
    return data


def _write_json(path: Path, data: Any) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _traces_dir(run_dir: Path) -> Path:
    """Return the traces/ directory for a run."""
    t = run_dir / "traces"
    return t if t.is_dir() else run_dir


def _attempt_numbers(task_dir: Path) -> list[int]:
    nums = []
    if not task_dir.exists():
        return nums
    for p in task_dir.iterdir():
        if p.is_dir():
            m = re.match(r"attempt_(\d+)$", p.name)
            if m:
                nums.append(int(m.group(1)))
    return sorted(nums)


def _agent_ids(attempt_dir: Path) -> list[str]:
    if not attempt_dir.exists():
        return []
    return sorted(p.stem for p in attempt_dir.glob("*.json") if p.stem != "steps")


def _attempt_cost_tokens(attempt_dir: Path, agent_ids: list[str]) -> tuple[float, int]:
    total_cost = 0.0
    total_tokens = 0
    for aid in agent_ids:
        data = _load_json(attempt_dir / f"{aid}.json")
        if data:
            s = data.get("summary", {})
            total_cost += s.get("total_cost_usd", 0.0)
            total_tokens += s.get("total_tokens", 0)
    return total_cost, total_tokens


def _model_provider(model_name: str) -> str:
    """Infer the provider from a model name string."""
    if model_name.startswith("claude"):
        return "anthropic"
    if (
        model_name.startswith("gpt")
        or model_name.startswith("o1")
        or model_name.startswith("o3")
        or model_name.startswith("o4")
    ):
        return "openai"
    if model_name.startswith("gemini"):
        return "gemini"
    return "other"


@dataclass
class RunData:
    """All pre-computed data for a single run, built in one pass over traces."""

    tasks: list[dict] = field(default_factory=list)
    total_cost: float = 0.0
    total_tokens: int = 0
    duration: float | None = None
    leaderboard: list[dict] = field(default_factory=list)
    token_stats: list[dict] = field(default_factory=list)
    cost_by_category: dict[str, float] = field(default_factory=dict)
    other_files: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class RunSummary:
    """Lightweight summary for the runs list page — only reads dag.json + orchestrator.json."""

    total_tasks: int = 0
    completed: int = 0
    failed: int = 0
    duration: float | None = None


def _load_run_summary(run_dir: Path) -> RunSummary:
    """Fast loader that reads only dag.json and orchestrator.json (2 files)."""
    dag = _load_json(run_dir / "dag.json")
    if not dag:
        return RunSummary()

    items = dag.get("items", [])
    tdir = _traces_dir(run_dir)
    orch = _load_json(tdir / "orchestrator.json")

    duration = None
    if orch:
        started = orch.get("started_at")
        ended = orch.get("ended_at")
        prior = orch.get("prior_duration_s", 0.0)
        if started and ended:
            duration = prior + (ended - started)
        elif started:
            import time

            duration = prior + (time.time() - started)

    return RunSummary(
        total_tasks=len(items),
        completed=sum(1 for t in items if t.get("status") == "completed"),
        failed=sum(1 for t in items if t.get("status") == "failed"),
        duration=duration,
    )


def _load_run_data(run_dir: Path, progress_cb: Callable[[int, int], None] | None = None) -> RunData:
    """Walk traces/ directory and aggregate cost/token stats from every JSON summary."""

    tdir = _traces_dir(run_dir)
    if not tdir.is_dir():
        return RunData()

    # Collect all JSON files under traces/
    all_jsons = sorted(tdir.rglob("*.json"))
    total_files = len(all_jsons)
    if progress_cb:
        progress_cb(0, total_files)

    # Load in batches to report progress
    batch_size = 50
    loaded: list[dict | None] = []
    for i in range(0, total_files, batch_size):
        batch = all_jsons[i : i + batch_size]
        loaded.extend(_POOL.map(_load_json, batch))
        if progress_cb:
            progress_cb(len(loaded), total_files)

    total_cost = 0.0
    total_tokens = 0
    earliest_start = None
    ended_at = None
    orch = None

    # Per-task aggregation: task_id -> {cost, tokens, attempts}
    task_stats: dict[str, dict] = {}
    # Worker leaderboard
    worker_stats: dict[str, dict] = {}
    # Per-attempt winner tracking: (task_id, attempt_num) -> winner_id
    winners: dict[tuple[str, int], str] = {}
    # Cost by component category
    category_costs: dict[str, float] = {
        "workers": 0.0,
        "reviewers": 0.0,
        "orchestrator": 0.0,
        "analyzers": 0.0,
        "readers": 0.0,
        "eval": 0.0,
        "merge_eval": 0.0,
        "other": 0.0,
    }
    other_files: list[tuple[str, float]] = []
    # Token stats per model
    model_stats: dict[str, dict] = defaultdict(
        lambda: {
            "total_input": 0,
            "total_output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "total_cost": 0.0,
        }
    )

    def _accumulate_token_stats(data: dict) -> None:
        for call in data.get("llm_calls", []):
            model = call.get("model", "unknown")
            ms = model_stats[model]
            ms["total_input"] += call.get("input_tokens", 0)
            ms["total_output"] += call.get("output_tokens", 0)
            ms["cache_read"] += call.get("cached_input_tokens", 0)
            ms["cache_creation"] += call.get("cache_creation_input_tokens", 0)
            ms["total_cost"] += call.get("cost_usd", 0) or 0

    # First pass: read steps.json for winner_ids
    for path, data in zip(all_jsons, loaded):
        if not data or path.name != "steps.json":
            continue
        parts = path.relative_to(tdir).parts  # e.g. ("tasks", task_id, "attempt_0", "steps.json")
        if len(parts) >= 3 and parts[0] == "tasks":
            task_id = parts[1]
            m = re.match(r"attempt_(\d+)$", parts[2])
            if m:
                winner = data.get("winner_id")
                if winner:
                    winners[(task_id, int(m.group(1)))] = winner

    # Second pass: aggregate costs from all trace files
    for path, data in zip(all_jsons, loaded):
        if not data:
            continue

        rel = path.relative_to(tdir)
        parts = rel.parts  # e.g. ("orchestrator.json",) or ("tasks", task_id, "attempt_0", "worker_0.json")

        # Skip steps.json — no cost data
        if path.name == "steps.json":
            continue

        s = data.get("summary", {})
        cost = s.get("total_cost_usd", 0.0)
        tokens = s.get("total_tokens", 0)

        # Orchestrator
        if parts == ("orchestrator.json",):
            orch = data
            total_cost += cost
            total_tokens += tokens
            category_costs["orchestrator"] += cost
            earliest_start = data.get("started_at")
            ended_at = data.get("ended_at")
            _accumulate_token_stats(data)
            continue

        # Task traces live under tasks/{task_id}/...
        if len(parts) < 3 or parts[0] != "tasks":
            # Non-task trace (reader, eval, etc.) — count cost but no per-task breakdown
            total_cost += cost
            total_tokens += tokens
            _accumulate_token_stats(data)
            # Categorize non-task traces
            rel = str(rel)
            if parts[0] == "readers":
                category_costs["readers"] += cost
            elif parts[0] in ("merge_eval", "merge_batches"):
                category_costs["merge_eval"] += cost
            elif parts[0] in ("eval", "judge"):
                category_costs["eval"] += cost
            else:
                category_costs["other"] += cost
                other_files.append((rel, cost))
            continue

        task_id = parts[1]
        if task_id not in task_stats:
            task_stats[task_id] = {"cost": 0.0, "tokens": 0, "attempts": set()}

        total_cost += cost
        total_tokens += tokens
        task_stats[task_id]["cost"] += cost
        task_stats[task_id]["tokens"] += tokens
        _accumulate_token_stats(data)

        # Categorize task traces
        fname = parts[-1]
        if fname == "analyzer.json":
            category_costs["analyzers"] += cost
        elif "worker" in fname:
            category_costs["workers"] += cost
        elif "reviewer" in fname:
            category_costs["reviewers"] += cost
        else:
            category_costs["other"] += cost
            other_files.append((str(rel), cost))

        # Agent trace inside an attempt dir: ("tasks", task_id, "attempt_N", "agent.json")
        if len(parts) == 4:
            m = re.match(r"attempt_(\d+)$", parts[2])
            if m:
                attempt_num = int(m.group(1))
                task_stats[task_id]["attempts"].add(attempt_num)
                aid = path.stem

                if "worker" in aid:
                    if aid not in worker_stats:
                        worker_stats[aid] = {"agent_id": aid, "worked": 0, "wins": 0, "cost": 0.0, "tokens": 0}
                    worker_stats[aid]["worked"] += 1
                    worker_stats[aid]["cost"] += cost
                    worker_stats[aid]["tokens"] += tokens
                    if aid == winners.get((task_id, attempt_num)):
                        worker_stats[aid]["wins"] += 1

    # --- Load archived usage snapshots from previous fresh runs ---
    archive_dir = run_dir / "archive"
    for snapshot_path in sorted(archive_dir.glob("usage_snapshot_*.json")):
        snap = _load_json(snapshot_path)
        if not snap:
            continue
        total_cost += snap.get("total_cost_usd", 0.0)
        total_tokens += snap.get("total_tokens", 0)
        for cat, val in snap.get("cost_by_category", {}).items():
            if cat in category_costs:
                category_costs[cat] += val
        for model, ms_snap in snap.get("model_stats", {}).items():
            ms = model_stats[model]
            ms["total_input"] += ms_snap.get("total_input", 0)
            ms["total_output"] += ms_snap.get("total_output", 0)
            ms["cache_read"] += ms_snap.get("cache_read", 0)
            ms["cache_creation"] += ms_snap.get("cache_creation", 0)
            ms["total_cost"] += ms_snap.get("total_cost", 0.0)

    # Duration
    duration = None
    if earliest_start and ended_at:
        prior = orch.get("prior_duration_s", 0.0) if orch else 0.0
        duration = prior + (ended_at - earliest_start)
    elif earliest_start:
        import time

        prior = orch.get("prior_duration_s", 0.0) if orch else 0.0
        duration = prior + (time.time() - earliest_start)

    # Build tasks list from dag.json, enriched with computed stats
    dag_data = _load_json(run_dir / "dag.json")
    items = dag_data.get("items", []) if dag_data else []
    tasks: list[dict] = []
    for t in items:
        tid = t["id"]
        ts = task_stats.get(tid, {})
        tasks.append(
            {
                **t,
                "num_attempts": len(ts.get("attempts", set())),
                "task_cost": ts.get("cost", 0.0),
                "task_tokens": ts.get("tokens", 0),
            }
        )

    # Finalize leaderboard
    leaderboard = sorted(worker_stats.values(), key=lambda x: x["wins"], reverse=True)
    for r in leaderboard:
        r["win_rate"] = r["wins"] / r["worked"] if r["worked"] else 0.0

    # Finalize token stats
    token_stats_list: list[dict] = []
    for model, ms in sorted(model_stats.items()):
        total_input = ms["total_input"]
        cache_read = ms["cache_read"]
        cache_creation = ms["cache_creation"]
        ms["model"] = model
        ms["provider"] = _model_provider(model)
        ms["regular_input"] = total_input - cache_read - cache_creation
        ms["cache_hit_pct"] = round(cache_read / total_input * 100, 1) if total_input else 0.0
        token_stats_list.append(ms)

    return RunData(
        tasks=tasks,
        total_cost=total_cost,
        total_tokens=total_tokens,
        duration=duration,
        leaderboard=leaderboard,
        token_stats=token_stats_list,
        cost_by_category=category_costs,
        other_files=sorted(other_files, key=lambda x: x[1], reverse=True)[:20],
    )


_LOC_CACHE_TTL = 900.0  # 15 minutes


def _count_loc(run_dir: Path) -> dict[str, int]:
    """Count lines of Lean code: total and code-only (no comments/blanks).

    Returns ``{"total": N, "code": M}`` where *code* excludes blank lines,
    single-line comments (``--``), and block comments (``/- … -/``).
    """
    code_root = run_dir / "code"
    lib_name = _read_lib_name(code_root)
    code_dir = code_root / lib_name if lib_name else None
    if not code_dir or not code_dir.exists():
        return {"total": 0, "code": 0}
    try:
        result = subprocess.run(
            ["find", str(code_dir), "-name", "*.lean", "-exec", "cat", "{}", "+"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {"total": 0, "code": 0}
        raw = result.stdout.decode("utf-8", errors="replace")
        total = raw.count("\n")
        # Count code lines (skip blanks, line comments, block comments)
        code_lines = 0
        in_block = 0  # nesting depth for /- … -/
        for line in raw.splitlines():
            stripped = line.strip()
            if in_block:
                # Check for nested /- and closing -/
                i = 0
                while i < len(stripped):
                    if stripped[i : i + 2] == "/-":
                        in_block += 1
                        i += 2
                    elif stripped[i : i + 2] == "-/":
                        in_block -= 1
                        i += 2
                        if in_block == 0:
                            # Rest of line after closing might have code
                            rest = stripped[i:].strip()
                            if rest and not rest.startswith("--"):
                                code_lines += 1
                            break
                    else:
                        i += 1
                continue
            if not stripped:
                continue
            if stripped.startswith("--"):
                continue
            if stripped.startswith("/-"):
                # Scan for nested opens and close on same line
                i = 2
                depth = 1
                while i < len(stripped):
                    if stripped[i : i + 2] == "/-":
                        depth += 1
                        i += 2
                    elif stripped[i : i + 2] == "-/":
                        depth -= 1
                        i += 2
                        if depth == 0:
                            rest = stripped[i:].strip()
                            if rest and not rest.startswith("--"):
                                code_lines += 1
                            break
                    else:
                        i += 1
                if depth > 0:
                    in_block = depth
                continue
            code_lines += 1
        return {"total": total, "code": code_lines}
    except (subprocess.TimeoutExpired, OSError):
        return {"total": 0, "code": 0}


def _is_lean_code(stripped: str, in_block: int) -> bool:
    """Return True if *stripped* is a code line (not blank/comment/inside block)."""
    if in_block:
        return False
    if not stripped:
        return False
    if stripped.startswith("--"):
        return False
    if stripped.startswith("/-"):
        # Check if it closes on the same line with code after
        depth = 1
        i = 2
        while i < len(stripped):
            if stripped[i : i + 2] == "/-":
                depth += 1
                i += 2
            elif stripped[i : i + 2] == "-/":
                depth -= 1
                i += 2
                if depth == 0:
                    rest = stripped[i:].strip()
                    return bool(rest) and not rest.startswith("--")
            else:
                i += 1
        return False
    return True


def _update_block_state(stripped: str, in_block: int) -> int:
    """Return updated block-comment nesting depth after processing *stripped*."""
    i = 0
    while i < len(stripped):
        if stripped[i : i + 2] == "/-":
            in_block += 1
            i += 2
        elif stripped[i : i + 2] == "-/":
            in_block = max(in_block - 1, 0)
            i += 2
        elif stripped[i : i + 2] == "--" and not in_block:
            break  # rest of line is a comment, stop scanning
        else:
            i += 1
    return in_block


def _mermaid_safe_id(raw: str) -> str:
    """Replace characters that break Mermaid node IDs."""
    import re

    return re.sub(r"[^a-zA-Z0-9_]", "_", raw)


def _build_mermaid(tasks: list[dict], run_name: str) -> str:
    status_colors = {
        "completed": "#16a34a",
        "in_progress": "#d97706",
        "failed": "#dc2626",
        "pending": "#64748b",
        "deleted": "#9ca3af",
    }
    lines = ["graph TD"]
    for t in tasks:
        tid = t["id"]
        safe = _mermaid_safe_id(tid)
        status = t.get("status", "pending")
        label = tid.replace('"', "#quot;")
        lines.append(f'    {safe}(["{label}"])')
        color = status_colors.get(status, "#64748b")
        lines.append(f"    style {safe} fill:{color},color:#fff,stroke:{color},stroke-width:2px,rx:12,ry:12")
    for t in tasks:
        for dep in t.get("depends_on", []):
            lines.append(f"    {_mermaid_safe_id(dep)} ---> {_mermaid_safe_id(t['id'])}")
    for t in tasks:
        safe = _mermaid_safe_id(t["id"])
        lines.append(f'    click {safe} "/run/{run_name}/traces/task/{t["id"]}" _self')
    return "\n".join(lines)


def _task_attempts(run_dir: Path, task_id: str) -> list[dict]:
    tdir = _traces_dir(run_dir)
    task_dir = tdir / "tasks" / task_id
    result = []
    for n in _attempt_numbers(task_dir):
        attempt_dir = task_dir / f"attempt_{n}"
        steps = _load_json(attempt_dir / "steps.json")
        aids = _agent_ids(attempt_dir)
        cost, tokens = _attempt_cost_tokens(attempt_dir, aids)

        started_at = None
        ended_at = None
        max_duration = 0.0
        for aid in aids:
            data = _load_json(attempt_dir / f"{aid}.json")
            if data:
                s_at = data.get("started_at")
                e_at = data.get("ended_at")
                if s_at and (started_at is None or s_at < started_at):
                    started_at = s_at
                if e_at and (ended_at is None or e_at > ended_at):
                    ended_at = e_at
                sd = data.get("summary", {}).get("total_duration_s", 0)
                if sd and sd > max_duration:
                    max_duration = sd
        if started_at and ended_at:
            duration = ended_at - started_at
        elif max_duration:
            duration = max_duration
        else:
            duration = None

        result.append(
            {
                "number": n,
                "final_status": steps.get("final_status", "unknown") if steps else "unknown",
                "winner_id": steps.get("winner_id") if steps else None,
                "agents": aids,
                "total_cost_usd": cost,
                "total_tokens": tokens,
                "total_duration_s": duration,
            }
        )
    return result


# ── Routes ────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def list_runs(request: Request):
    runs_dir = get_runs_dir()
    runs: list[dict] = []
    if not runs_dir.exists():
        return templates.TemplateResponse(request, "runs.html", {"runs": runs})

    run_dirs = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir() and ((d / "dag.json").exists() or (d / "traces").is_dir())],
        reverse=True,
    )
    run_filter = _get_run_filter()
    if run_filter:
        run_dirs = [d for d in run_dirs if d.name == run_filter]

    runs = [{"name": d.name} for d in run_dirs]
    return templates.TemplateResponse(request, "runs.html", {"runs": runs})


_stats_generating: set[str] = set()  # run names currently generating stats
_stats_progress: dict[str, dict] = {}  # run_name -> {"done": N, "total": M}


@app.post("/api/run/{run_name}/escalation/{index}/dismiss")
async def dismiss_escalation(run_name: str, index: int):
    """Mark an escalation as dismissed by setting dismissed=true in the JSONL."""
    run_dir = get_runs_dir() / run_name
    esc_path = run_dir / "escalations.jsonl"
    if not esc_path.exists():
        raise HTTPException(404, "No escalations file")
    lines = esc_path.read_text().splitlines()
    if index < 0 or index >= len(lines):
        raise HTTPException(404, f"Escalation index {index} out of range")
    entry = json.loads(lines[index])
    entry["dismissed"] = True
    lines[index] = json.dumps(entry)
    esc_path.write_text("\n".join(lines) + "\n")
    return {"status": "dismissed"}


@app.get("/api/run/{run_name}/escalations/count")
async def escalation_count(run_name: str):
    """Return count of active (non-dismissed) escalations by severity."""
    run_dir = get_runs_dir() / run_name
    esc_path = run_dir / "escalations.jsonl"
    critical = 0
    warning = 0
    if esc_path.exists():
        try:
            for line in esc_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("dismissed"):
                    continue
                if entry.get("severity") == "critical":
                    critical += 1
                else:
                    warning += 1
        except Exception:
            pass
    return {"critical": critical, "warning": warning, "total": critical + warning}


@app.post("/api/run/{run_name}/stats")
async def run_stats(run_name: str):
    """Fire-and-forget stats generation — starts computation in background.

    Writes results to stats.json in the run directory. Returns immediately
    so the user can navigate away without cancelling the computation.
    """
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    if run_name in _stats_generating:
        return {"status": "already_running"}

    def _compute_and_save() -> None:
        def _progress(done: int, total: int) -> None:
            _stats_progress[run_name] = {"done": done, "total": total}

        try:
            run_data = _load_run_data(run_dir, progress_cb=_progress)
            loc = _count_loc(run_dir)
            task_costs = {
                t["id"]: {"cost": t["task_cost"], "tokens": t["task_tokens"], "num_attempts": t["num_attempts"]}
                for t in run_data.tasks
                if t.get("task_cost", 0) > 0 or t.get("num_attempts", 0) > 0
            }
            result = {
                "total_cost": run_data.total_cost,
                "total_tokens": run_data.total_tokens,
                "loc": loc,
                "task_costs": task_costs,
                "leaderboard": run_data.leaderboard,
                "cost_by_category": run_data.cost_by_category,
                "other_files": run_data.other_files,
                "token_stats": run_data.token_stats,
            }
            with open(run_dir / "stats.json", "w") as f:
                json.dump(result, f, indent=2)
        except Exception:
            logger.exception("Stats generation failed for run %s", run_name)
        finally:
            _stats_generating.discard(run_name)
            _stats_progress.pop(run_name, None)

    _stats_generating.add(run_name)
    _POOL.submit(_compute_and_save)
    return {"status": "started"}


@app.get("/api/run/{run_name}/stats/status")
async def run_stats_status(run_name: str):
    """Check if stats generation is complete."""
    ready = run_name not in _stats_generating
    progress = _stats_progress.get(run_name)
    return {"ready": ready, "progress": progress}


def _load_merge_batches(run_dir: Path) -> list[dict]:
    """Load all merge batches — both queue-only and evaluated.

    Scans ``traces/merge_batches/*/`` for batch trace folders, then joins
    with ``reports/merge_reports/{hash}/report.json`` when present.  Batches
    that never landed (all agents rejected) appear as queue-only entries.

    Falls back to legacy ``traces/merge_eval/`` + ``reports/merge_reports/``
    layout for older runs.
    """
    reports_dir = run_dir / "reports" / "merge_reports"
    tdir = _traces_dir(run_dir)
    batches_dir = tdir / "merge_batches"
    legacy_dir = tdir / "merge_eval"

    # Collect all batch hashes from both traces and reports.
    seen_hashes: set[str] = set()
    hash_dirs: list[tuple[str, str]] = []  # (hash, source: "batches"|"legacy"|"reports_only")

    if batches_dir.exists():
        for d in batches_dir.iterdir():
            if d.is_dir():
                seen_hashes.add(d.name)
                hash_dirs.append((d.name, "batches"))

    # Reports that have no trace folder (e.g. older runs or eval-only)
    if reports_dir.exists():
        for d in reports_dir.iterdir():
            if d.is_dir() and d.name not in seen_hashes:
                seen_hashes.add(d.name)
                if legacy_dir.exists() and (legacy_dir / d.name).is_dir():
                    hash_dirs.append((d.name, "legacy"))
                else:
                    hash_dirs.append((d.name, "reports_only"))

    # Sort reverse-chronologically by hash (approximation — hashes aren't ordered,
    # but within a run the reports dirs are created in order).
    hash_dirs.sort(key=lambda x: x[0], reverse=True)

    results: list[dict] = []
    for hash_name, source in hash_dirs:
        # --- Load queue steps ---
        queue_steps: list[dict] = []
        if source == "batches":
            trace_dir = batches_dir / hash_name
            trace_subdir = "merge_batches"
        elif source == "legacy":
            trace_dir = legacy_dir / hash_name
            trace_subdir = "merge_eval"
        else:
            trace_dir = None
            trace_subdir = ""

        total_cost = 0.0
        traces: list[dict] = []
        if trace_dir and trace_dir.exists():
            steps_data = _load_json(trace_dir / "steps.json")
            if steps_data:
                queue_steps = steps_data.get("steps", [])

            for tf in sorted(trace_dir.rglob("*.json")):
                if tf.stem == "steps":
                    continue
                trace_data = _load_json(tf)
                if not trace_data:
                    continue
                summary = trace_data.get("summary", {})
                cost = summary.get("total_cost_usd", 0.0)
                total_cost += cost
                rel_path = tf.relative_to(trace_dir)
                traces.append(
                    {
                        "name": str(rel_path.with_suffix("")),
                        "path": f"{trace_subdir}/{hash_name}/{rel_path}",
                        "turns": summary.get("total_turns", 0),
                        "cost": cost,
                        "duration_s": summary.get("duration_s", 0.0),
                    }
                )

        # --- Load report (if present) ---
        report_path = reports_dir / hash_name / "report.json"
        report = _load_json(report_path) if report_path.exists() else None

        has_report = report is not None
        merge_info: dict = {}
        passed = failed = total = 0
        pass_rate = None
        details: list = []

        if report:
            merge_info = report.get("merge", {})
            stmts = report.get("statements", {})
            stmt_summary = stmts.get("summary", {})
            details = stmts.get("details", [])
            passed = stmt_summary.get("passed", 0)
            failed = stmt_summary.get("failed", 0)
            total = stmt_summary.get("total", 0)
            pass_rate = stmt_summary.get("pass_rate")

        # --- Determine batch status from steps ---
        batch_status = "unknown"
        if queue_steps:
            landed = any("land_batch" in s.get("function", "") and s.get("success") for s in queue_steps)
            bisected = any("bisect" in s.get("function", "") for s in queue_steps)
            if landed and has_report:
                batch_status = "evaluated"
            elif landed:
                batch_status = "landed"
            elif bisected:
                batch_status = "bisected"
            else:
                batch_status = "rejected"
        elif has_report:
            batch_status = "evaluated"

        post_hash = merge_info.get("post_hash", hash_name)

        # Extract pre_hash from report or from land_batch step args
        pre_hash = merge_info.get("pre_hash")
        if not pre_hash and queue_steps:
            for s in queue_steps:
                if "land_batch" in s.get("function", "") and s.get("success"):
                    pre_hash = (s.get("args_summary") or {}).get("pre_hash")
                    if pre_hash:
                        break

        results.append(
            {
                "post_hash": post_hash,
                "pre_hash": pre_hash,
                "task_id": merge_info.get("task_id"),
                "batch_status": batch_status,
                "has_report": has_report,
                "passed": passed,
                "failed": failed,
                "total": total,
                "pass_rate": pass_rate,
                "all_passed": failed == 0 and total > 0,
                "has_failures": failed > 0,
                "total_cost": total_cost,
                "traces": traces,
                "queue_steps": queue_steps,
                "details": details,
            }
        )

    # Sort by first step timestamp (most recent first), falling back to hash.
    def _sort_key(b: dict) -> float:
        steps = b.get("queue_steps", [])
        if steps:
            return -(steps[0].get("timestamp", 0.0))
        return 0.0

    results.sort(key=_sort_key)
    return results


def _compute_progress_data(run_dir: Path, progress_cb: Callable[[str, str], None] | None = None) -> dict:
    """Build multi-series progress data from traces, steps, and goal events.

    Uses an incremental disk cache at ``progress_data.json``. On each call
    only scans trace files not seen in the previous build.

    Returns::

        {
            "usage_timeline": [[ts, cum_cost, cum_tokens], ...],
            "goals":          [[ts, goals_completed], ...],
            "tasks":          [[ts, tasks_completed], ...],
            "rounds":         [[ts, avg_rounds], ...],
            "goals_total":    int,
            "sessions":       [{start, stop}, ...],
        }

    The frontend combines these with the usage timeline to produce any
    (x-axis, y-axis) combination.
    """
    cache_path = run_dir / "progress_data.json"
    tdir = _traces_dir(run_dir)

    # Load previous cache
    cache = _load_json(cache_path) or {}
    prev_usage_events: list[list] = cache.get("_usage_events", [])
    prev_trace_files: set[str] = set(cache.get("_trace_files", []))
    prev_steps_files: set[str] = set(cache.get("_steps_files", []))
    # Incremental: task completions and round counts from previously scanned steps
    prev_task_completions: list[list] = cache.get("_task_completions", [])  # [[ts, task_id]]
    prev_round_events: list[list] = cache.get("_round_events", [])  # [[ts, turns]]

    def _progress(step: str, detail: str = "", pct: int = 0) -> None:
        if progress_cb:
            progress_cb(step, detail, pct)

    # --- 1. Scan trace files from archive/traces/ first (complete history),
    #         then traces/ for any not yet archived (active agents) ---
    _progress("traces", "discovering trace files", 0)
    usage_events = list(prev_usage_events)
    current_trace_files: set[str] = set()

    archive_dir = run_dir / "archive"
    archive_traces_dir = archive_dir / "traces"

    # Scan archive first, then live — use relative path (without prefix) to dedup
    all_scan: list[tuple[str, Path]] = []  # (rel_path, abs_path)
    seen_rels: set[str] = set()

    # Archive traces (complete history)
    if archive_traces_dir.is_dir():
        for p in sorted(archive_traces_dir.rglob("*.json")):
            if p.name != "steps.json":
                rel = str(p.relative_to(archive_traces_dir))
                if rel not in seen_rels:
                    seen_rels.add(rel)
                    all_scan.append((rel, p))

    # Live traces (only files not already seen from archive)
    if tdir.is_dir():
        for p in sorted(tdir.rglob("*.json")):
            if p.name != "steps.json":
                rel = str(p.relative_to(tdir))
                if rel not in seen_rels:
                    seen_rels.add(rel)
                    all_scan.append((rel, p))

    # Note: archive/orchestrator_*.json are snapshots from --fresh runs,
    # but archive/traces/orchestrator.json already accumulates the full
    # history via ArchiveTraceStore, so we skip the snapshots to avoid
    # double-counting.

    new_trace_count = sum(1 for key, _ in all_scan if key not in prev_trace_files)
    scanned = 0
    _progress("traces", f"scanning traces (0/{new_trace_count} new)", 0)
    for key, path in all_scan:
        if key in prev_trace_files:
            current_trace_files.add(key)
            continue
        scanned += 1
        if scanned % 20 == 0:
            pct = round(scanned / max(new_trace_count, 1) * 60)
            _progress("traces", f"scanning traces ({scanned}/{new_trace_count} new)", pct)
        data = _load_json(path)
        if not data or "summary" not in data:
            continue
        current_trace_files.add(key)
        ts = data.get("ended_at") or data.get("started_at")
        cost = data["summary"].get("total_cost_usd", 0.0)
        tokens = data["summary"].get("total_tokens", 0)
        if cost > 0 or tokens > 0:
            # Traces without any timestamp go at the end (use inf so they sort last)
            usage_events.append([ts or float("inf"), cost, tokens])
    _progress("traces", f"scanned {scanned} new traces", 60)

    # Build cumulative usage timeline
    usage_events.sort(key=lambda e: e[0])
    # Replace inf timestamps with the last real timestamp (or now)
    last_real_ts = _time.time()
    for evt in usage_events:
        if evt[0] != float("inf") and evt[0] > 0:
            last_real_ts = max(last_real_ts, evt[0])
    for evt in usage_events:
        if evt[0] == float("inf"):
            evt[0] = last_real_ts

    usage_timeline: list[list] = []
    running_cost = 0.0
    running_tokens = 0
    for ts, c, t in usage_events:
        running_cost += c
        running_tokens += t
        usage_timeline.append([ts, round(running_cost, 4), running_tokens])

    # --- 2. Scan steps.json files for task completions + round counts ---
    task_completions = list(prev_task_completions)
    round_events = list(prev_round_events)
    current_steps_files: set[str] = set()

    steps_scan: list[tuple[str, Path]] = []
    seen_steps_rels: set[str] = set()
    # Archive first, then live — dedup by relative path
    for d in [archive_traces_dir, tdir]:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("steps.json")):
            rel = str(p.relative_to(d))
            if rel not in seen_steps_rels:
                seen_steps_rels.add(rel)
                steps_scan.append((rel, p))

    new_steps_count = sum(1 for key, _ in steps_scan if key not in prev_steps_files)
    steps_scanned = 0
    _progress("steps", f"scanning steps (0/{new_steps_count} new)", 60)
    for key, path in steps_scan:
        if key in prev_steps_files:
            current_steps_files.add(key)
            continue
        steps_scanned += 1
        if steps_scanned % 10 == 0:
            pct = 60 + round(steps_scanned / max(new_steps_count, 1) * 30)
            _progress("steps", f"scanning steps ({steps_scanned}/{new_steps_count} new)", pct)
        data = _load_json(path)
        if not data:
            continue
        current_steps_files.add(key)

        steps = data.get("steps", [])
        if not steps:
            continue

        # Completion timestamp = last step's end time
        last_step = steps[-1]
        completion_ts = last_step.get("timestamp", 0.0) + last_step.get("duration_ms", 0.0) / 1000.0

        # Task completion
        if data.get("final_status") == "success":
            # Extract task_id from key: {prefix}tasks/{task_id}/attempt_{n}/steps.json
            rel = key.split(":", 1)[1] if ":" in key else key
            parts = Path(rel).parts
            if len(parts) >= 2 and parts[0] == "tasks":
                task_completions.append([completion_ts, parts[1]])

        # Round count from worker traces in the same attempt directory
        # Extract attempt number from path: {prefix}tasks/{task_id}/attempt_{n}/steps.json
        attempt_num = 0
        rel = key.split(":", 1)[1] if ":" in key else key
        parts = Path(rel).parts
        if len(parts) >= 3:
            m = re.match(r"attempt_(\d+)$", parts[2])
            if m:
                attempt_num = int(m.group(1))

        attempt_dir = path.parent
        for agent_trace_path in sorted(attempt_dir.glob("*.json")):
            if agent_trace_path.name == "steps.json":
                continue
            if "worker" not in agent_trace_path.stem:
                continue
            agent_data = _load_json(agent_trace_path)
            if not agent_data or "summary" not in agent_data:
                continue
            turns = agent_data["summary"].get("total_turns", 0)
            agent_ended = agent_data.get("ended_at") or completion_ts
            if turns > 0:
                round_events.append([agent_ended, turns, attempt_num])

    # --- 4. Build goal timeline ---
    _progress("goals", "reading goal events", 90)
    events_path = run_dir / "goal_events.jsonl"
    goal_states: dict[int, str] = {}
    raw_goal_events: list[dict] = []

    if events_path.exists():
        try:
            with open(events_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw_goal_events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    goals_data = _load_json(run_dir / "goals.json")
    goals_total = len(goals_data.get("items", [])) if goals_data else 0

    raw_goal_events.sort(key=lambda e: e.get("timestamp", 0.0))
    goals_series: list[list] = []
    for event in raw_goal_events:
        gid = event.get("goal_id")
        status = event.get("status", "")
        ts = event.get("timestamp", 0.0)
        if gid is not None:
            goal_states[gid] = status
        completed = sum(1 for s in goal_states.values() if s == "completed")
        goals_series.append([ts, completed])

    # --- 5. Build tasks-completed series (cumulative, deduplicated) ---
    task_completions.sort(key=lambda e: e[0])
    seen_tasks: set[str] = set()
    tasks_series: list[list] = []
    for ts, tid in task_completions:
        if tid not in seen_tasks:
            seen_tasks.add(tid)
            tasks_series.append([ts, len(seen_tasks)])

    # --- 6. Build rounds histogram data, grouped by attempt number ---
    # Each round_event is [ts, turns, attempt_num]
    round_events.sort(key=lambda e: e[0])
    # Raw turn counts per attempt: {attempt_num: [turns, turns, ...]}
    rounds_by_attempt: dict[int, list[int]] = {}
    for _ts, turns, anum in round_events:
        rounds_by_attempt.setdefault(anum, []).append(turns)

    sessions = _load_sessions(tdir)

    # --- 7. Build LOC over time from git history ---
    _progress("loc", "computing lines of code history", 92)
    loc_series: list[list] = []  # [[timestamp, cumulative_loc], ...]
    code_loc_series: list[list] = []  # [[timestamp, cumulative_code_loc], ...]
    code_dir = run_dir / "code"
    if code_dir.is_dir():
        try:
            # git log with patch for .lean files, oldest first
            result = subprocess.run(
                ["git", "log", "--reverse", "--format=COMMIT %at", "-p", "--", "*.lean"],
                cwd=code_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                running_loc = 0
                running_code = 0
                current_ts = 0
                current_adds = 0
                current_dels = 0
                code_adds = 0
                code_dels = 0
                in_block = 0  # nesting depth for /- … -/
                in_hunk = False

                for line in result.stdout.splitlines():
                    if line.startswith("COMMIT "):
                        # Flush previous commit
                        if current_ts > 0:
                            running_loc += current_adds - current_dels
                            running_code += code_adds - code_dels
                            loc_series.append([current_ts, max(running_loc, 0)])
                            code_loc_series.append([current_ts, max(running_code, 0)])
                        current_ts = int(line.split()[1])
                        current_adds = 0
                        current_dels = 0
                        code_adds = 0
                        code_dels = 0
                        in_block = 0
                        in_hunk = False
                        continue

                    # Track hunk headers to know we're in diff content
                    if line.startswith("@@"):
                        in_hunk = True
                        continue
                    if (
                        line.startswith("diff ")
                        or line.startswith("index ")
                        or line.startswith("--- ")
                        or line.startswith("+++ ")
                    ):
                        in_hunk = False
                        continue

                    if not in_hunk:
                        continue

                    if line.startswith("+"):
                        current_adds += 1
                        content = line[1:].strip()
                        if _is_lean_code(content, in_block):
                            code_adds += 1
                        # Update block comment state for added lines
                        in_block = _update_block_state(content, in_block)
                    elif line.startswith("-"):
                        current_dels += 1
                        content = line[1:].strip()
                        if _is_lean_code(content, in_block):
                            code_dels += 1
                        in_block = _update_block_state(content, in_block)
                    # context lines (no prefix) — update block state
                    elif not line.startswith("\\"):
                        content = line.strip() if line else ""
                        in_block = _update_block_state(content, in_block)

                # Flush last commit
                if current_ts > 0:
                    running_loc += current_adds - current_dels
                    running_code += code_adds - code_dels
                    loc_series.append([current_ts, max(running_loc, 0)])
                    code_loc_series.append([current_ts, max(running_code, 0)])
        except (subprocess.TimeoutExpired, OSError):
            pass

    # --- 8. Build task outcome histogram (completed vs deleted by attempt count) ---
    #         Also collect per-task costs grouped by outcome for the cost chart.
    task_outcomes: dict[str, dict[int, int]] = {}  # {status: {attempt_count: num_tasks}}
    task_cost_by_outcome: dict[str, list[float]] = {}  # {status: [cost, cost, ...]}
    dag_data = _load_json(run_dir / "dag.json")
    stats_data = _load_json(run_dir / "stats.json")
    task_costs_map = stats_data.get("task_costs", {}) if stats_data else {}
    if dag_data:
        for item in dag_data.get("items", []):
            status = item.get("status", "")
            if status not in ("completed", "deleted"):
                continue
            attempts = item.get("attempts", 0)
            if attempts < 1:
                attempts = 1
            task_outcomes.setdefault(status, {}).setdefault(attempts, 0)
            task_outcomes[status][attempts] += 1
            # Collect cost for this task
            tc = task_costs_map.get(item["id"], {})
            cost = tc.get("cost", 0.0)
            if cost > 0:
                task_cost_by_outcome.setdefault(status, []).append(round(cost, 4))

    # --- 9. Write cache ---
    _progress("saving", "writing cache", 95)
    result = {
        "usage_timeline": usage_timeline,
        "goals": goals_series,
        "tasks": tasks_series,
        "rounds_by_attempt": {str(k): v for k, v in sorted(rounds_by_attempt.items())},
        "loc": loc_series,
        "code_loc": code_loc_series,
        "goals_total": goals_total,
        "sessions": sessions,
        "task_outcomes": {s: {str(k): v for k, v in sorted(d.items())} for s, d in task_outcomes.items()},
        "task_cost_by_outcome": {s: sorted(costs) for s, costs in task_cost_by_outcome.items()},
    }
    cache_data = {
        **result,
        "_usage_events": usage_events,
        "_trace_files": sorted(current_trace_files),
        "_steps_files": sorted(current_steps_files),
        "_task_completions": task_completions,
        "_round_events": round_events,
    }
    try:
        tmp = cache_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(cache_data, f)
        os.replace(tmp, cache_path)
    except OSError:
        logger.warning("Failed to write progress_data cache")

    return result


_cvg_generating: set[str] = set()
_cvg_progress: dict[str, dict] = {}  # run_name -> {"step": str, "detail": str}
_cvg_error: dict[str, str] = {}  # run_name -> error message


def _load_sessions(traces_dir: Path) -> list[dict]:
    """Load session start/stop pairs from ``traces/sessions.jsonl``."""
    sessions_path = traces_dir / "sessions.jsonl"
    if not sessions_path.exists():
        return []

    events: list[dict] = []
    try:
        with open(sessions_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []

    # Pair start/stop events
    sessions: list[dict] = []
    current_start: float | None = None
    for ev in events:
        if ev.get("type") == "start":
            current_start = ev.get("timestamp")
        elif ev.get("type") == "stop" and current_start is not None:
            sessions.append({"start": current_start, "stop": ev.get("timestamp")})
            current_start = None

    # If pipeline is currently running (start without stop)
    if current_start is not None:
        sessions.append({"start": current_start, "stop": None})

    return sessions


@app.post("/api/run/{run_name}/progress-data")
async def build_progress_data(run_name: str, fresh: bool = False):
    """Fire-and-forget: build progress data in background.

    Pass ``?fresh=true`` to delete the cache and rebuild from scratch.
    """
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")
    if run_name in _cvg_generating:
        return {"status": "already_running"}

    if fresh:
        cache_path = run_dir / "progress_data.json"
        cache_path.unlink(missing_ok=True)

    def _build() -> None:
        def _on_progress(step: str, detail: str, pct: int = 0) -> None:
            _cvg_progress[run_name] = {"step": step, "detail": detail, "pct": pct}

        try:
            _cvg_error.pop(run_name, None)
            _compute_progress_data(run_dir, progress_cb=_on_progress)
        except Exception as e:
            logger.exception("Progress data build failed for %s", run_name)
            _cvg_error[run_name] = str(e)
        finally:
            _cvg_generating.discard(run_name)
            _cvg_progress.pop(run_name, None)

    _cvg_generating.add(run_name)
    _POOL.submit(_build)
    return {"status": "started"}


@app.get("/api/run/{run_name}/progress-data/status")
async def progress_data_status(run_name: str):
    """Check if progress data build is complete."""
    ready = run_name not in _cvg_generating
    progress = _cvg_progress.get(run_name)
    error = _cvg_error.pop(run_name, None)
    return {"ready": ready, "progress": progress, "error": error}


@app.get("/api/run/{run_name}/progress-data")
async def api_progress_data(run_name: str):
    """Return cached progress data from disk."""
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    cache_path = run_dir / "progress_data.json"
    data = await asyncio.to_thread(_load_json, cache_path)
    if not data:
        return {
            "usage_timeline": [],
            "goals": [],
            "tasks": [],
            "rounds_by_attempt": {},
            "goals_total": 0,
            "sessions": [],
        }

    # Strip internal cache keys from response
    return {k: v for k, v in data.items() if not k.startswith("_")}


@app.post("/api/run/{run_name}/export-plot-data")
async def export_plot_data(run_name: str):
    """Export all plot data combinations as a single JSON to ~/ablations/."""
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    cache_path = run_dir / "progress_data.json"
    data = await asyncio.to_thread(_load_json, cache_path)
    if not data:
        return {"error": "No progress data available. Build it first."}

    usage_tl = data.get("usage_timeline", [])
    sessions = data.get("sessions", [])
    tl_timestamps = [u[0] for u in usage_tl]
    t0 = usage_tl[0][0] if usage_tl else 0
    if sessions and sessions[0].get("start"):
        t0 = min(t0, sessions[0]["start"])

    def _usage_at(ts: float) -> tuple[float, float]:
        if not tl_timestamps:
            return 0.0, 0.0
        idx = bisect.bisect_right(tl_timestamps, ts) - 1
        if idx < 0:
            return 0.0, 0.0
        return usage_tl[idx][1], usage_tl[idx][2]

    def _runtime_at(ts: float) -> float:
        return ts - t0 if t0 else 0.0

    def _active_at(ts: float) -> float:
        if not sessions:
            return _runtime_at(ts)
        active = 0.0
        for s in sessions:
            start = s["start"]
            stop = s.get("stop") or ts
            if ts <= start:
                break
            end = min(ts, stop)
            active += max(0.0, end - start)
        return active

    def _x_values(ts: float) -> dict:
        cost, tokens = _usage_at(ts)
        return {
            "cumulative_tokens": tokens,
            "cumulative_cost_usd": cost,
            "wall_clock_runtime_s": _runtime_at(ts),
            "active_runtime_s": _active_at(ts),
        }

    # Build all y-axis series, each row contains all x-axis values
    goals_total = data.get("goals_total", 1) or 1
    series: dict[str, Any] = {}

    for y_key, raw_key, transform in [
        ("goals_completed", "goals", lambda e: e[1]),
        ("goals_completed_pct", "goals", lambda e: round(e[1] / goals_total * 100, 2)),
        ("tasks_completed", "tasks", lambda e: e[1]),
        ("lines_of_code", "loc", lambda e: e[1]),
    ]:
        raw = data.get(raw_key, [])
        if raw:
            series[y_key] = [{**_x_values(e[0]), y_key: transform(e)} for e in raw]

    if data.get("rounds_by_attempt"):
        series["rounds_by_attempt"] = data["rounds_by_attempt"]

    export = {
        "run_name": run_name,
        "goals_total": goals_total,
        "series": series,
    }

    out_dir = Path.home() / "ablations"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_name}_plot_data.json"
    await asyncio.to_thread(_write_json, out_path, export)
    return {"path": str(out_path)}


@app.get("/run/{run_name}/merge-batches", response_class=HTMLResponse)
async def view_merge_batches(request: Request, run_name: str):
    """Merge evaluation history with per-eval details and traces."""
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    evals = await asyncio.to_thread(_load_merge_batches, run_dir)

    return templates.TemplateResponse(
        request,
        "merge_batches.html",
        {
            "request": request,
            "run_name": run_name,
            "evals": evals,
        },
    )


@app.get("/api/run/{run_name}/dag")
async def run_dag(run_name: str):
    """On-demand DAG mermaid endpoint."""
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    dag_data = _load_json(run_dir / "dag.json") or {}
    items = dag_data.get("items", [])
    mermaid = _build_mermaid(items, run_name)
    return {"mermaid": mermaid}


@app.get("/run/{run_name}", response_class=HTMLResponse)
async def view_dag(request: Request, run_name: str):
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    # Lightweight: only reads dag.json + orchestrator.json (2 files).
    dag_data = _load_json(run_dir / "dag.json") or {}
    items = dag_data.get("items", [])

    tdir = _traces_dir(run_dir)
    orch = _load_json(tdir / "orchestrator.json")
    has_orchestrator = orch is not None

    duration = None
    if orch:
        started = orch.get("started_at")
        ended = orch.get("ended_at")
        prior = orch.get("prior_duration_s", 0.0)
        if started and ended:
            duration = prior + (ended - started)
        elif started:
            import time

            duration = prior + (time.time() - started)

    # Load cached stats.json if available (written by /api/run/{run_name}/stats).
    cached_stats = _load_json(run_dir / "stats.json")

    # Load escalations (JSONL file, one JSON object per line).
    escalations: list[dict] = []
    esc_path = run_dir / "escalations.jsonl"
    if esc_path.exists():
        try:
            for idx, line in enumerate(esc_path.read_text().splitlines()):
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    if not entry.get("dismissed"):
                        entry["index"] = idx
                        escalations.append(entry)
        except Exception:
            pass

    return templates.TemplateResponse(
        request,
        "dag.html",
        {
            "request": request,
            "run_name": run_name,
            "tasks": items,
            "duration": duration,
            "has_orchestrator": has_orchestrator,
            "cached_stats": cached_stats,
            "escalations": escalations,
        },
    )


@app.get("/run/{run_name}/escalations", response_class=HTMLResponse)
async def view_escalations(request: Request, run_name: str):
    """Dedicated escalations page with filtering and reply capability."""
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    escalations: list[dict] = []
    esc_path = run_dir / "escalations.jsonl"
    if esc_path.exists():
        try:
            for idx, line in enumerate(esc_path.read_text().splitlines()):
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    entry["index"] = idx
                    escalations.append(entry)
        except Exception:
            pass

    # Extract unique agent IDs for the source filter
    agent_ids = sorted({e.get("agent_id", "unknown") for e in escalations if e.get("agent_id")})

    return templates.TemplateResponse(
        request,
        "escalations.html",
        {
            "request": request,
            "run_name": run_name,
            "escalations": escalations,
            "agent_ids": agent_ids,
        },
    )


@app.get("/run/{run_name}/traces/task/{task_id}/attempt/{n}", response_class=HTMLResponse)
async def view_attempt(request: Request, run_name: str, task_id: str, n: int):
    run_dir = get_runs_dir() / run_name
    tdir = _traces_dir(run_dir)
    attempt_dir = tdir / "tasks" / task_id / f"attempt_{n}"
    if not attempt_dir.exists():
        raise HTTPException(404, f"Attempt {n} not found for task {task_id}")

    def _load() -> dict:
        steps = _load_json(attempt_dir / "steps.json") or {}
        aids = _agent_ids(attempt_dir)
        cost, tokens = _attempt_cost_tokens(attempt_dir, aids)

        agents = []
        task_description = ""
        for aid in aids:
            data = _load_json(attempt_dir / f"{aid}.json") or {}
            s = data.get("summary", {})
            agents.append(
                {
                    "agent_id": aid,
                    "final_status": data.get("final_status", "unknown"),
                    "total_turns": data.get("total_turns", 0),
                    "total_tokens": s.get("total_tokens", 0),
                    "total_cost_usd": s.get("total_cost_usd", 0.0),
                    "is_winner": aid == steps.get("winner_id"),
                }
            )
            if not task_description:
                for msg in data.get("messages", []):
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str) and content.strip():
                            task_description = content
                            break

        return {
            "steps": steps,
            "cost": cost,
            "tokens": tokens,
            "agents": agents,
            "task_description": task_description,
        }

    ctx = await asyncio.to_thread(_load)

    return templates.TemplateResponse(
        request,
        "attempt.html",
        {
            "request": request,
            "run_name": run_name,
            "task_id": task_id,
            "n": n,
            "final_status": ctx["steps"].get("final_status", "unknown"),
            "winner_id": ctx["steps"].get("winner_id"),
            "total_cost_usd": ctx["cost"],
            "total_tokens": ctx["tokens"],
            "agents": ctx["agents"],
            "steps": ctx["steps"].get("steps", []),
            "task_description": ctx["task_description"],
        },
    )


@app.get("/run/{run_name}/traces/task/{task_id:path}", response_class=HTMLResponse)
async def view_task(request: Request, run_name: str, task_id: str):
    task_id = task_id.split("/attempt/")[0]
    run_dir = get_runs_dir() / run_name

    def _load() -> dict:
        dag_data = _load_json(run_dir / "dag.json") or {}
        task = next((t for t in dag_data.get("items", []) if t["id"] == task_id), None)

        attempts = _task_attempts(run_dir, task_id)
        task_cost = sum(a.get("total_cost_usd", 0.0) for a in attempts)
        task_tokens = sum(a.get("total_tokens", 0) for a in attempts)
        tdir = _traces_dir(run_dir)
        has_analyzer = (tdir / "tasks" / task_id / "analyzer.json").exists()

        analyzer = _load_json(tdir / "tasks" / task_id / "analyzer.json")
        if analyzer:
            s = analyzer.get("summary", {})
            task_cost += s.get("total_cost_usd", 0.0)
            task_tokens += s.get("total_tokens", 0)

        return {
            "task": task,
            "attempts": attempts,
            "task_cost": task_cost,
            "task_tokens": task_tokens,
            "has_analyzer": has_analyzer,
        }

    ctx = await asyncio.to_thread(_load)
    if ctx["task"] is None:
        raise HTTPException(404, f"Task not found: {task_id}")

    return templates.TemplateResponse(
        request,
        "task.html",
        {
            "request": request,
            "run_name": run_name,
            "task": ctx["task"],
            "attempts": ctx["attempts"],
            "task_cost": ctx["task_cost"],
            "task_tokens": ctx["task_tokens"],
            "has_analyzer": ctx["has_analyzer"],
        },
    )


@app.get("/run/{run_name}/usage", response_class=HTMLResponse)
async def view_usage(request: Request, run_name: str):
    """Token usage and cost breakdown for a run."""
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    cached_stats = _load_json(run_dir / "stats.json")

    return templates.TemplateResponse(
        request,
        "usage.html",
        {
            "request": request,
            "run_name": run_name,
            "cached_stats": cached_stats,
        },
    )


@app.get("/run/{run_name}/insights", response_class=HTMLResponse)
async def view_insights(request: Request, run_name: str):
    """Skills, reports, git commit history, and token usage breakdown for a run."""
    run_dir = get_runs_dir() / run_name
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_name}")

    def _load_insights() -> dict:
        def _load_skills() -> list[dict]:
            skills = []
            skills_dir = run_dir / "skills"
            if skills_dir.exists():
                for f in sorted(skills_dir.rglob("*.md")):
                    try:
                        content = f.read_text()
                        skills.append({"path": str(f.relative_to(skills_dir)), "content": content})
                    except Exception:
                        pass
            return skills

        def _load_reports() -> list[dict]:
            reports = []
            reports_dir = run_dir / "reports" / "task_reports"
            if reports_dir.exists():
                for f in sorted(reports_dir.glob("*.json")):
                    data = _load_json(f)
                    if data:
                        reports.append({"name": f.stem, "data": json.dumps(data, indent=2)})
            return reports

        def _load_git() -> tuple[str, list[dict]]:
            code_dir = run_dir / "code"
            if not code_dir.exists():
                return "", []
            r1 = subprocess.run(
                ["git", "log", "--oneline", "--graph", "-40"],
                cwd=code_dir,
                capture_output=True,
                text=True,
            )
            r2 = subprocess.run(
                ["git", "log", "--format=%H %h %s", "-40"],
                cwd=code_dir,
                capture_output=True,
                text=True,
            )
            git_log = r1.stdout if r1.returncode == 0 else ""
            git_commits: list[dict] = []
            if r2.returncode == 0:
                for line in r2.stdout.strip().splitlines():
                    parts = line.split(" ", 2)
                    if len(parts) >= 3:
                        git_commits.append({"sha": parts[0], "short_sha": parts[1], "message": parts[2]})
            return git_log, git_commits

        # Run all four in parallel via the thread pool
        skills_fut = _POOL.submit(_load_skills)
        reports_fut = _POOL.submit(_load_reports)
        git_fut = _POOL.submit(_load_git)
        loc_fut = _POOL.submit(_count_loc, run_dir)

        skills = skills_fut.result()
        reports = reports_fut.result()
        git_log, git_commits = git_fut.result()
        loc = loc_fut.result()

        # Task outcomes & cost by outcome — cheap reads from dag.json/stats.json
        task_outcomes: dict[str, dict[str, int]] = {}
        task_cost_by_outcome: dict[str, list[float]] = {}
        dag_data = _load_json(run_dir / "dag.json")
        stats_data = _load_json(run_dir / "stats.json")
        task_costs_map = stats_data.get("task_costs", {}) if stats_data else {}
        if dag_data:
            for item in dag_data.get("items", []):
                status = item.get("status", "")
                if status not in ("completed", "deleted"):
                    continue
                attempts = item.get("attempts", 0)
                if attempts < 1:
                    attempts = 1
                task_outcomes.setdefault(status, {}).setdefault(attempts, 0)
                task_outcomes[status][attempts] += 1
                tc = task_costs_map.get(item["id"], {})
                cost = tc.get("cost", 0.0)
                if cost > 0:
                    task_cost_by_outcome.setdefault(status, []).append(round(cost, 4))
        task_outcomes_out = {s: {str(k): v for k, v in sorted(d.items())} for s, d in task_outcomes.items()}
        task_cost_out = {s: sorted(costs) for s, costs in task_cost_by_outcome.items()}

        return {
            "skills": skills,
            "reports": reports,
            "git_log": git_log,
            "git_commits": git_commits,
            "loc": loc,
            "task_outcomes": task_outcomes_out,
            "task_cost_by_outcome": task_cost_out,
        }

    # Load cached JSON files if available.

    ctx = await asyncio.to_thread(_load_insights)

    # Progress chart state
    building = run_name in _cvg_generating
    build_progress = _cvg_progress.get(run_name)
    progress_data = _load_json(run_dir / "progress_data.json")
    # Strip internal cache keys
    if progress_data:
        progress_data = {k: v for k, v in progress_data.items() if not k.startswith("_")}

    return templates.TemplateResponse(
        request,
        "insights.html",
        {
            "request": request,
            "run_name": run_name,
            "skills": ctx["skills"],
            "reports": ctx["reports"],
            "git_log": ctx["git_log"],
            "git_commits": ctx["git_commits"],
            "loc": ctx["loc"],
            "building": building,
            "build_progress": build_progress,
            "progress_data": progress_data,
            "task_outcomes": ctx["task_outcomes"],
            "task_cost_by_outcome": ctx["task_cost_by_outcome"],
        },
    )


def _parse_diff(raw: str) -> list[dict]:
    """Parse a unified diff into per-file structured data.

    Returns a list of file dicts, each containing:
        filename, old_filename (if renamed), additions, deletions,
        and hunks — each hunk has header + lines with type/old_no/new_no.
    """
    files: list[dict] = []
    current_file: dict | None = None
    current_hunk: dict | None = None
    old_no = new_no = 0

    for line in raw.splitlines():
        if line.startswith("diff --git"):
            # Start a new file
            parts = line.split(" b/", 1)
            filename = parts[1] if len(parts) > 1 else line
            current_file = {
                "filename": filename,
                "old_filename": None,
                "additions": 0,
                "deletions": 0,
                "hunks": [],
                "is_binary": False,
                "is_new": False,
                "is_deleted": False,
            }
            files.append(current_file)
            current_hunk = None
        elif current_file is not None and line.startswith("new file"):
            current_file["is_new"] = True
        elif current_file is not None and line.startswith("deleted file"):
            current_file["is_deleted"] = True
        elif current_file is not None and line.startswith("rename from "):
            current_file["old_filename"] = line[len("rename from ") :]
        elif current_file is not None and line.startswith("Binary files"):
            current_file["is_binary"] = True
        elif current_file is not None and line.startswith("@@"):
            # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
            if m:
                old_no = int(m.group(1))
                new_no = int(m.group(2))
                rest = m.group(3)
            else:
                old_no = new_no = 0
                rest = ""
            current_hunk = {"header": line, "context_label": rest.strip(), "lines": []}
            current_file["hunks"].append(current_hunk)
        elif current_hunk is not None:
            if line.startswith("+"):
                current_hunk["lines"].append({"type": "add", "old_no": None, "new_no": new_no, "text": line[1:]})
                new_no += 1
                current_file["additions"] += 1
            elif line.startswith("-"):
                current_hunk["lines"].append({"type": "del", "old_no": old_no, "new_no": None, "text": line[1:]})
                old_no += 1
                current_file["deletions"] += 1
            elif line.startswith("\\"):
                current_hunk["lines"].append({"type": "meta", "old_no": None, "new_no": None, "text": line})
            else:
                current_hunk["lines"].append(
                    {
                        "type": "ctx",
                        "old_no": old_no,
                        "new_no": new_no,
                        "text": line[1:] if line.startswith(" ") else line,
                    }
                )
                old_no += 1
                new_no += 1

    return files


@app.get("/run/{run_name}/commit/{sha}", response_class=HTMLResponse)
async def view_commit(request: Request, run_name: str, sha: str):
    """Show the diff for a single git commit."""
    run_dir = get_runs_dir() / run_name
    code_dir = run_dir / "code"
    if not code_dir.exists():
        raise HTTPException(404, "Code directory not found")

    # Validate sha is hex only (prevent injection)
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha):
        raise HTTPException(400, "Invalid commit SHA")

    def _load() -> dict | None:
        result = subprocess.run(
            ["git", "show", "--stat", "--format=%H%n%h%n%an%n%s%n%aI", sha],
            cwd=code_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        lines = result.stdout.split("\n", 5)
        diff_result = subprocess.run(
            ["git", "show", "--format=", "--patch", sha],
            cwd=code_dir,
            capture_output=True,
            text=True,
        )
        raw_diff = diff_result.stdout if diff_result.returncode == 0 else ""
        diff_files = _parse_diff(raw_diff)

        return {
            "full_sha": lines[0] if len(lines) > 0 else sha,
            "short_sha": lines[1] if len(lines) > 1 else sha[:7],
            "author": lines[2] if len(lines) > 2 else "",
            "message": lines[3] if len(lines) > 3 else "",
            "date": lines[4] if len(lines) > 4 else "",
            "diff_files": diff_files,
        }

    ctx = await asyncio.to_thread(_load)
    if ctx is None:
        raise HTTPException(404, f"Commit not found: {sha}")

    return templates.TemplateResponse(
        request,
        "commit.html",
        {
            "run_name": run_name,
            "full_sha": ctx["full_sha"],
            "short_sha": ctx["short_sha"],
            "author": ctx["author"],
            "message": ctx["message"],
            "date": ctx["date"],
            "diff_files": ctx["diff_files"],
            "total_additions": sum(f["additions"] for f in ctx["diff_files"]),
            "total_deletions": sum(f["deletions"] for f in ctx["diff_files"]),
            "total_files": len(ctx["diff_files"]),
        },
    )


@app.get("/run/{run_name}/compare/{ref_range}", response_class=HTMLResponse)
async def view_compare(request: Request, run_name: str, ref_range: str):
    """Show the diff between two commits (base..head)."""
    run_dir = get_runs_dir() / run_name
    code_dir = run_dir / "code"
    if not code_dir.exists():
        raise HTTPException(404, "Code directory not found")

    if ".." not in ref_range:
        raise HTTPException(400, "Expected format: base..head")
    base, head = ref_range.split("..", 1)

    for sha in (base, head):
        if not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha):
            raise HTTPException(400, f"Invalid commit SHA: {sha}")

    def _load() -> dict | None:
        # Get short info for both commits
        def _commit_info(sha: str) -> dict:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H%n%h%n%s", sha],
                cwd=code_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return {"full_sha": sha, "short_sha": sha[:7], "message": ""}
            lines = result.stdout.strip().splitlines()
            return {
                "full_sha": lines[0] if len(lines) > 0 else sha,
                "short_sha": lines[1] if len(lines) > 1 else sha[:7],
                "message": lines[2] if len(lines) > 2 else "",
            }

        base_info = _commit_info(base)
        head_info = _commit_info(head)

        diff_result = subprocess.run(
            ["git", "diff", base, head],
            cwd=code_dir,
            capture_output=True,
            text=True,
        )
        if diff_result.returncode != 0:
            return None

        diff_files = _parse_diff(diff_result.stdout)
        return {"base": base_info, "head": head_info, "diff_files": diff_files}

    ctx = await asyncio.to_thread(_load)
    if ctx is None:
        raise HTTPException(404, f"Could not diff {base}..{head}")

    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "run_name": run_name,
            "base": ctx["base"],
            "head": ctx["head"],
            "diff_files": ctx["diff_files"],
            "total_additions": sum(f["additions"] for f in ctx["diff_files"]),
            "total_deletions": sum(f["deletions"] for f in ctx["diff_files"]),
            "total_files": len(ctx["diff_files"]),
        },
    )


@app.get("/run/{run_name}/agent-trace/{trace_path:path}", response_class=HTMLResponse)
async def view_agent_trace(run_name: str, trace_path: str):
    run_dir = get_runs_dir() / run_name
    tdir = _traces_dir(run_dir)
    trace_file = tdir / trace_path
    if not trace_file.exists():
        raise HTTPException(404, f"Trace not found: {trace_path}")

    def _load() -> str | None:
        data = _load_json(trace_file)
        if not data:
            return None
        return generate_html(data)

    html = await asyncio.to_thread(_load)
    if not html:
        raise HTTPException(500, "Failed to load trace")

    # Inject auto-refresh
    refresh_script = """<script>
let lastLen = document.body.innerHTML.length;
setInterval(async () => {
    try {
        const r = await fetch(location.href);
        const t = await r.text();
        if (t.length !== lastLen) { lastLen = t.length; document.open(); document.write(t); document.close(); }
    } catch(e) {}
}, 10000);
</script>"""

    html = html.replace("</body>", refresh_script + "</body>")
    return HTMLResponse(html)


# ── Hardware monitoring ───────────────────────────────────────────


@app.get("/run/{run_name}/hardware", response_class=HTMLResponse)
async def view_hardware(request: Request, run_name: str):
    """Hardware monitoring page — per-node CPU, memory, Lean processes."""
    import httpx

    control_urls = _discover_control_urls(run_name)
    nodes: list[dict] = []

    if control_urls:

        async def _fetch_node(client: httpx.AsyncClient, rank: int, url: str) -> dict:
            try:
                resp = await client.get(f"{url}/metrics", timeout=3.0)
                if resp.status_code == 200:
                    data = resp.json()
                    data["status"] = "online"
                    data["rank"] = rank
                    return data
                return {"rank": rank, "status": "error", "hostname": f"rank-{rank}"}
            except (httpx.ConnectError, httpx.TimeoutException):
                return {"rank": rank, "status": "unreachable", "hostname": f"rank-{rank}"}

        async with httpx.AsyncClient() as client:
            nodes = list(
                await asyncio.gather(*[_fetch_node(client, rank, url) for rank, url in sorted(control_urls.items())])
            )

    # Aggregated summary
    total_cpu_allocated = sum(n.get("cpu_count_allocated", 0) for n in nodes if n["status"] == "online")
    total_cpu_total = sum(n.get("cpu_count_total", 0) for n in nodes if n["status"] == "online")
    total_mem_allocated_gb = sum(n.get("memory_allocated_gb", 0) for n in nodes if n["status"] == "online")
    total_mem_user_gb = sum(n.get("memory_user_gb", 0) for n in nodes if n["status"] == "online")
    total_lean_procs = sum(len(n.get("lean_processes", [])) for n in nodes if n["status"] == "online")
    online_count = sum(1 for n in nodes if n["status"] == "online")

    return templates.TemplateResponse(
        request,
        "hardware.html",
        {
            "run_name": run_name,
            "nodes": nodes,
            "total_cpu_allocated": total_cpu_allocated,
            "total_cpu_total": total_cpu_total,
            "total_mem_allocated_gb": total_mem_allocated_gb,
            "total_mem_user_gb": total_mem_user_gb,
            "total_lean_procs": total_lean_procs,
            "online_count": online_count,
            "total_nodes": len(nodes),
        },
    )


# ── Registry proxy ───────────────────────────────────────────────


def _discover_control_url(run_name: str) -> str | None:
    """Discover the primary control plane URL (rank 0) for shutdown."""
    urls = _discover_control_urls(run_name)
    return urls.get(0)


@app.post("/api/shutdown")
async def proxy_shutdown(run: str | None = None):
    """Proxy shutdown request to the pipeline's control plane."""
    import httpx

    if not run:
        raise HTTPException(400, "Missing 'run' query parameter")
    control_url = _discover_control_url(run)
    if not control_url:
        raise HTTPException(503, "No control plane found — is the pipeline running?")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{control_url}/shutdown", timeout=5.0)
    except httpx.ConnectError:
        raise HTTPException(503, "Control plane not reachable — is the pipeline running?")
    except httpx.TimeoutException:
        raise HTTPException(504, "Control plane timed out")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.json().get("detail", "Control plane error"))
    return resp.json()


@app.post("/api/eval")
async def trigger_eval(run: str | None = None, concurrency: int = 1000):
    """Submit an eval job against the given run as a background subprocess."""
    if not run:
        raise HTTPException(400, "Missing 'run' query parameter")
    run_dir = get_runs_dir() / run
    if not (run_dir / "code").exists():
        raise HTTPException(404, f"Run directory not found or missing code/: {run_dir}")

    fort_root = Path(__file__).resolve().parent.parent.parent
    venv_python = fort_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        raise HTTPException(500, f".venv python not found at {venv_python}")

    # Write eval logs to a file the visualizer can read
    eval_log_dir = run_dir / "reports" / "eval_reports"
    eval_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = eval_log_dir / "eval.log"

    # Create marker so the visualizer knows an eval is pending.
    evaluating_marker = eval_log_dir / ".evaluating"
    evaluating_marker.touch()

    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [
            str(venv_python),
            "-u",
            "-m",
            "autoform.bot.eval_process",
            "--run-path",
            str(run_dir),
            "--concurrency",
            str(concurrency),
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(fort_root),
    )

    # Store PID so stop_eval can find and kill it
    pid_path = eval_log_dir / ".eval_pid"
    pid_path.write_text(str(proc.pid))

    def _wait_and_cleanup():
        proc.wait()
        log_file.close()
        evaluating_marker.unlink(missing_ok=True)
        pid_path.unlink(missing_ok=True)

    threading.Thread(target=_wait_and_cleanup, daemon=True).start()
    return {"status": "submitted", "run": run, "pid": proc.pid}


@app.post("/api/eval/stop")
async def stop_eval(run: str | None = None):
    """Stop a running local eval process."""
    if not run:
        raise HTTPException(400, "Missing 'run' query parameter")
    run_dir = get_runs_dir() / run
    eval_log_dir = run_dir / "reports" / "eval_reports"
    pid_path = eval_log_dir / ".eval_pid"
    marker = eval_log_dir / ".evaluating"

    if not pid_path.exists():
        # No PID file — can't kill the process, but clean up stale marker
        marker.unlink(missing_ok=True)
        return {"status": "cleaned", "run": run, "pid": None}

    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # Already dead
    except PermissionError:
        raise HTTPException(403, f"Cannot kill PID {pid} — permission denied")

    pid_path.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    return {"status": "stopped", "run": run, "pid": pid}


@app.post("/api/eval/transfer-to-goals")
async def transfer_to_goals(run: str | None = None):
    """Transfer latest eval report results into goals.json."""
    if not run:
        raise HTTPException(400, "Missing 'run' query parameter")
    run_dir = get_runs_dir() / run
    if not (run_dir / "goals.json").exists():
        raise HTTPException(404, "goals.json not found for this run")

    from autoform.bot.eval_process import transfer_report_to_goals

    try:
        result = await asyncio.to_thread(transfer_report_to_goals, run_dir)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return result


async def get_eval_log(run: str | None = None, offset: int = 0):
    """Return eval log contents from a given byte offset."""
    if not run:
        raise HTTPException(400, "Missing 'run' query parameter")
    log_path = get_runs_dir() / run / "reports" / "eval_reports" / "eval.log"
    if not log_path.exists():
        marker = get_runs_dir() / run / "reports" / "eval_reports" / ".evaluating"
        return {"log": "", "offset": 0, "done": not marker.exists()}
    size = log_path.stat().st_size
    if offset >= size:
        # Check if eval is still running
        eval_dir = get_runs_dir() / run / "reports" / "eval_reports"
        still_running = False
        if eval_dir.exists():
            # Top-level marker (created by trigger_eval before slurm job starts)
            if (eval_dir / ".evaluating").exists():
                still_running = True
            else:
                # Per-commit marker (created by eval_process during execution)
                still_running = any(
                    (d / ".evaluating").exists() for d in eval_dir.iterdir() if d.is_dir() and d.name != "latest"
                )
        return {"log": "", "offset": offset, "done": not still_running}
    with open(log_path, "rb") as f:
        f.seek(offset)
        raw = f.read(1024 * 1024)  # Cap at 1MB per request
    content = raw.decode("utf-8", errors="replace")
    return {"log": content, "offset": offset + len(raw), "done": False}


@app.get("/api/eval-progress")
async def get_eval_progress(run: str | None = None):
    """Return the current partial report.json for a running eval.

    The pipeline writes report.json progressively every 5 statements.
    Returns the report data plus progress info, or 404 if no eval is running.
    """
    if not run:
        raise HTTPException(400, "Missing 'run' query parameter")
    eval_dir = get_runs_dir() / run / "reports" / "eval_reports"
    if not eval_dir.exists():
        raise HTTPException(404, "No eval reports directory")

    # Find the in-progress eval (has .evaluating marker)
    target_dir = None
    for d in sorted(eval_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
        if not d.is_dir() or d.name == "latest":
            continue
        if (d / ".evaluating").exists():
            target_dir = d
            break

    if target_dir is None:
        # No in-progress eval — check latest
        latest = eval_dir / "latest"
        if latest.is_symlink():
            target_dir = latest.resolve()
        else:
            raise HTTPException(404, "No eval in progress or completed")

    report_path = target_dir / "report.json"
    if not report_path.exists():
        return {
            "commit": target_dir.name,
            "in_progress": True,
            "progress": {"completed": 0, "total": 0},
            "report": None,
        }

    data = _load_json(report_path)
    if not data:
        return {
            "commit": target_dir.name,
            "in_progress": True,
            "progress": {"completed": 0, "total": 0},
            "report": None,
        }

    still_running = (target_dir / ".evaluating").exists()
    progress = data.get("progress", {})
    return {
        "commit": target_dir.name,
        "in_progress": still_running,
        "progress": progress,
        "report": data,
    }


@app.post("/api/agent/{agent_id}/message")
async def proxy_send_message(agent_id: str, request: Request, run: str | None = None):
    """Proxy chat messages to the correct registry server."""
    import httpx

    registry_urls = _discover_registry_urls(run) if run else {}
    if not registry_urls:
        # Fall back to legacy single registry
        url = _get_registry_url()
        if not url:
            raise HTTPException(503, "No registry configured")
        registry_urls = {0: url}

    target = _route_agent_to_registry(agent_id, registry_urls)
    if not target:
        raise HTTPException(404, f"No registry found for agent: {agent_id}")

    body = await request.json()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{target}/agent/{agent_id}/message",
                json=body,
                timeout=5.0,
            )
    except httpx.ConnectError:
        raise HTTPException(503, "Registry not reachable — is the pipeline running?")
    except httpx.TimeoutException:
        raise HTTPException(504, "Registry timed out")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.json().get("detail", "Registry error"))
    return resp.json()


# ── Live agent routes ────────────────────────────────────────────


def _agent_type(agent_id: str) -> str:
    """Infer agent role from its ID."""
    if "worker" in agent_id:
        return "worker"
    if "reviewer" in agent_id:
        return "reviewer"
    if "orchestrator" in agent_id:
        return "orchestrator"
    if "trace_analyzer" in agent_id:
        return "analyzer"
    return "other"


@app.get("/run/{run_name}/live", response_class=HTMLResponse)
async def live_agents(request: Request, run_name: str):
    """Agents Lobby — list all agents with node, status, and metadata."""
    agents: list[dict] = []
    node_status: dict[str, str] = {}
    registry_urls = _discover_registry_urls(run_name)
    if registry_urls:
        import httpx

        async def _fetch_registry(client: httpx.AsyncClient, rank: int, url: str) -> tuple[str, str, list[dict]]:
            node_label = f"Node {rank}" if rank > 0 else "Coordinator"
            try:
                resp = await client.get(f"{url}/agents/active", timeout=3.0)
                if resp.status_code == 200:
                    node_agents = []
                    for a in resp.json().get("agents", []):
                        if isinstance(a, str):
                            a = {"id": a, "status": "unknown", "turns": 0, "pending": 0}
                        a["node"] = node_label
                        a["type"] = _agent_type(a["id"])
                        node_agents.append(a)
                    return node_label, "online", node_agents
            except httpx.ConnectError:
                return node_label, "unreachable", []
            except Exception:
                return node_label, "error", []
            return node_label, "error", []

        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *[_fetch_registry(client, rank, url) for rank, url in sorted(registry_urls.items())]
            )
            for label, status, node_agents in results:
                node_status[label] = status
                agents.extend(node_agents)
    return templates.TemplateResponse(
        request,
        "live.html",
        {
            "agents": agents,
            "node_status": node_status,
            "run_name": run_name,
            "registry_connected": bool(agents) or bool(registry_urls),
        },
    )


@app.get("/run/{run_name}/live/{agent_id}", response_class=HTMLResponse)
async def live_agent_chat(request: Request, run_name: str, agent_id: str):
    """Live conversation view + chat for a specific agent."""
    return templates.TemplateResponse(
        request,
        "live_chat.html",
        {
            "agent_id": agent_id,
            "run_name": run_name,
        },
    )


@app.get("/api/agent/{agent_id}/messages")
async def proxy_get_messages(agent_id: str, run: str | None = None):
    """Proxy message history from the correct registry server."""
    import httpx

    registry_urls = _discover_registry_urls(run) if run else []
    if not registry_urls:
        url = _get_registry_url()
        if not url:
            raise HTTPException(503, "No registry configured")
        registry_urls = [url]

    target = _route_agent_to_registry(agent_id, registry_urls)
    if not target:
        raise HTTPException(404, f"No registry found for agent: {agent_id}")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{target}/agent/{agent_id}/messages", timeout=5.0)
    except httpx.ConnectError:
        raise HTTPException(503, "Registry not reachable — is the pipeline running?")
    except httpx.TimeoutException:
        raise HTTPException(504, "Registry timed out")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.json().get("detail", "Registry error"))
    return resp.json()


@app.get("/run/{run_name}/eval", response_class=HTMLResponse)
async def view_eval(request: Request, run_name: str):
    """Eval reports — trigger evaluations and browse results."""
    run_dir = get_runs_dir() / run_name
    eval_dir = run_dir / "reports" / "eval_reports"

    def _load() -> dict:
        # Detect which commit is "latest" via symlink
        latest_commit = None
        latest_link = eval_dir / "latest"
        if latest_link.is_symlink():
            latest_commit = latest_link.resolve().name

        # Scan for eval reports
        reports: list[dict] = []
        evaluating = False
        if eval_dir.exists():
            for d in sorted(eval_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
                if not d.is_dir() or d.name == "latest":
                    continue
                report_json = d / "report.json"
                marker = d / ".evaluating"
                if marker.exists():
                    # Clean up stale markers: if PID file is missing or process is dead
                    pid_file = d / ".eval_pid"
                    if pid_file.exists():
                        try:
                            pid = int(pid_file.read_text().strip())
                            os.kill(pid, 0)  # Check if process is alive
                        except (ProcessLookupError, ValueError):
                            marker.unlink(missing_ok=True)
                            pid_file.unlink(missing_ok=True)
                            marker = d / ".evaluating"  # re-check below
                        except PermissionError:
                            pass  # Process exists but we can't signal it
                    # Also check the top-level marker
                    top_marker = eval_dir / ".evaluating"
                    top_pid = eval_dir / ".eval_pid"
                    if top_marker.exists() and not top_pid.exists():
                        top_marker.unlink(missing_ok=True)
                if marker.exists():
                    evaluating = True
                if not report_json.exists():
                    if marker.exists():
                        reports.append(
                            {
                                "commit": d.name,
                                "in_progress": True,
                                "is_latest": d.name == latest_commit,
                            }
                        )
                    continue
                data = _load_json(report_json)
                if not data:
                    continue
                summary = data.get("statements", {}).get("summary", {})
                repo = data.get("repo", {})
                checkpoint = data.get("checkpoint", {})
                details = data.get("statements", {}).get("details", [])
                progress = data.get("progress", {})

                # Categorize targets
                passed = [d2 for d2 in details if d2.get("passed")]
                not_covered = [
                    d2
                    for d2 in details
                    if not d2.get("passed")
                    and (
                        d2.get("match_confidence") == "not_found"
                        or not d2.get("lean_declaration")
                        or d2.get("lean_declaration") == "-"
                    )
                ]
                issues = [d2 for d2 in details if not d2.get("passed") and d2 not in not_covered]

                reports.append(
                    {
                        "commit": d.name,
                        "timestamp": checkpoint.get("timestamp", ""),
                        "compiles": repo.get("compiles", False),
                        "total": summary.get("total", 0),
                        "passed": len(passed),
                        "failed": len(issues),
                        "not_covered": len(not_covered),
                        "pass_rate": summary.get("pass_rate", 0),
                        "faithfulness": summary.get("faithfulness"),
                        "proof_integrity": summary.get("proof_integrity"),
                        "code_quality": summary.get("code_quality"),
                        "is_latest": d.name == latest_commit,
                        "in_progress": marker.exists(),
                        "progress": progress,
                        "details": details,
                    }
                )

        latest_report = next((r for r in reports if r.get("is_latest") and not r.get("in_progress")), None)
        return {"reports": reports, "latest": latest_report, "evaluating": evaluating}

    ctx = await asyncio.to_thread(_load)

    return templates.TemplateResponse(
        request,
        "eval.html",
        {
            "run_name": run_name,
            "reports": ctx["reports"],
            "latest": ctx["latest"],
            "evaluating": ctx["evaluating"],
        },
    )


# ---------------------------------------------------------------------------
# Dependency graph explorer
# ---------------------------------------------------------------------------


@app.get("/run/{run_name}/depgraph", response_class=HTMLResponse)
async def view_depgraph(request: Request, run_name: str):
    """Dependency graph explorer — structural analysis of the Lean codebase."""
    run_dir = get_runs_dir() / run_name
    dg_path = run_dir / "dependency_graph.json"
    building_marker = run_dir / ".building_depgraph"

    def _load() -> dict:
        building = building_marker.exists()
        build_status = ""
        if building:
            log_path = run_dir / "depgraph_build.log"
            if log_path.exists():
                try:
                    text = log_path.read_text().strip()
                    lines = [line for line in text.splitlines() if line.strip()]
                    build_status = lines[-1] if lines else "Starting..."
                except OSError:
                    build_status = "Starting..."
        return {
            "building": building,
            "build_status": build_status,
            "has_graph": dg_path.exists(),
        }

    ctx = await asyncio.to_thread(_load)

    return templates.TemplateResponse(
        request,
        "depgraph.html",
        {
            "run_name": run_name,
            **ctx,
        },
    )


@app.get("/api/run/{run_name}/depgraph/data")
async def api_depgraph_data(run_name: str):
    """Serve the dependency graph JSON for client-side rendering."""
    run_dir = get_runs_dir() / run_name
    dg_path = run_dir / "dependency_graph.json"
    if not dg_path.exists():
        raise HTTPException(404, "No dependency graph found")

    from fastapi.responses import FileResponse

    return FileResponse(dg_path, media_type="application/json")


@app.post("/api/run/{run_name}/depgraph/build")
async def trigger_depgraph_build(run_name: str):
    """Trigger dependency graph build as a background process."""
    run_dir = get_runs_dir() / run_name
    code_root = run_dir / "code"
    if not code_root.exists():
        raise HTTPException(404, f"No code/ directory in run: {run_dir}")

    building_marker = run_dir / ".building_depgraph"
    if building_marker.exists():
        return {"status": "already_building"}

    lib_name = _read_lib_name(code_root)
    if not lib_name:
        raise HTTPException(400, "Cannot determine module prefix from lakefile.toml")

    fort_root = Path(__file__).resolve().parent.parent.parent
    venv_python = fort_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        raise HTTPException(500, f".venv python not found at {venv_python}")

    building_marker.touch()

    # Fire and forget: run the graph builder as a subprocess
    log_path = run_dir / "depgraph_build.log"
    script = f"""\
import asyncio, sys, time
sys.path.insert(0, "{fort_root}")
from pathlib import Path

async def main():
    print("Running lake build to ensure oleans are up to date...", flush=True)
    import subprocess
    build_result = subprocess.run(
        ["lake", "build"],
        cwd="{code_root}",
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if build_result.returncode != 0:
        print(f"WARNING: lake build failed (rc={{build_result.returncode}}), proceeding anyway...", flush=True)
        print(build_result.stderr[-500:] if build_result.stderr else "", flush=True)

    print("Running Lean metaprogram...", flush=True)
    t0 = time.time()

    from autoform.eval.dependency_graph.builder import build_raw_graph
    nodes = await build_raw_graph(
        repo_dir=Path("{code_root}"),
        module_prefix="{lib_name}",
        import_module="{lib_name}",
        timeout=3600,
    )
    print(f"Parsed {{len(nodes)}} declarations in {{time.time()-t0:.0f}}s", flush=True)

    print("Applying graph-level tags...", flush=True)
    from autoform.eval.dependency_graph.tagger import apply_graph_tags
    nodes = apply_graph_tags(nodes)

    print("Computing transitive dependencies...", flush=True)
    from autoform.eval.dependency_graph import _compute_transitive_deps, DependencyGraph
    nodes = _compute_transitive_deps(nodes)

    print("Saving dependency_graph.json...", flush=True)
    graph = DependencyGraph(nodes=nodes)
    graph.save(Path("{run_dir}/dependency_graph.json"))
    print(f"Done — {{graph.size}} nodes saved", flush=True)

asyncio.run(main())
"""

    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [str(venv_python), "-u", "-c", script],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(fort_root),
    )

    def _wait_and_cleanup():
        proc.wait()
        log_file.close()
        building_marker.unlink(missing_ok=True)

    threading.Thread(target=_wait_and_cleanup, daemon=True).start()
    return {"status": "submitted", "run": run_name, "pid": proc.pid}


@app.get("/run/{run_name}/goals", response_class=HTMLResponse)
async def view_goals(request: Request, run_name: str):
    """Goal tracker — formalization target pass/fail/pending status."""
    run_dir = get_runs_dir() / run_name
    goals_path = run_dir / "goals.json"
    goals_data = _load_json(goals_path)

    goals: list[dict] = []
    if goals_data:
        for item in goals_data.get("items", []):
            goals.append(item)

    completed = sum(1 for g in goals if g.get("status") == "completed")
    failed = sum(1 for g in goals if g.get("status") == "failed")
    pending = sum(1 for g in goals if g.get("status") == "pending")
    failed_axioms = sum(
        1 for g in goals if g.get("status") == "failed" and (g.get("metadata") or {}).get("failure_reason") == "axioms"
    )
    failed_faithfulness = sum(
        1
        for g in goals
        if g.get("status") == "failed" and (g.get("metadata") or {}).get("failure_reason") == "faithfulness"
    )
    failed_compilation = sum(
        1
        for g in goals
        if g.get("status") == "failed" and (g.get("metadata") or {}).get("failure_reason") == "compilation"
    )

    return templates.TemplateResponse(
        request,
        "goals.html",
        {
            "run_name": run_name,
            "goals": goals,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "total": len(goals),
            "failed_axioms": failed_axioms,
            "failed_faithfulness": failed_faithfulness,
            "failed_compilation": failed_compilation,
        },
    )


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Autoform Visualizer V1")
    parser.add_argument("--runs-dir", type=Path, default=None, help="Directory containing run subdirectories")
    parser.add_argument("--registry", type=str, default=None, help="Registry API URL (e.g. http://localhost:8822)")
    parser.add_argument("--run-filter", type=str, default=None, help="Only serve this run (used by hub gateway)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    # Set module-level config via the module object
    if args.runs_dir:
        resolved = str(args.runs_dir.expanduser().resolve())
        os.environ["VIZV1_RUNS_DIR"] = resolved
        print(f"Runs directory: {resolved}")

    if args.registry:
        os.environ["VIZV1_REGISTRY_URL"] = args.registry.rstrip("/")
        print(f"Registry API: {args.registry.rstrip('/')}")

    if args.run_filter:
        os.environ["VIZV1_RUN_FILTER"] = args.run_filter
        print(f"Run filter: {args.run_filter}")

    uvicorn.run("autoform.visualizer.app:app", host=args.host, port=args.port, workers=1)
