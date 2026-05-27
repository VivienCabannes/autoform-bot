# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Control plane API — pipeline-level operations (shutdown, status, metrics).

Started on every rank as a background thread. The visualizer discovers
control servers via ``urls.json`` and proxies requests to them.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable

import psutil
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

app = FastAPI(title="Pipeline Control API")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

_shutdown_callback: Callable[[], None] | None = None
_rank: int = 0
_start_time: float = time.monotonic()

_LEAN_PROCESS_NAMES = {"lean", "lake", "lean-lsp-mcp", "repl"}


@app.post("/shutdown")
async def shutdown():
    """Initiate graceful pipeline shutdown."""
    logger.warning("POST /shutdown received — initiating graceful shutdown")
    if _shutdown_callback is None:
        return {"status": "error", "detail": "No shutdown callback registered"}
    _shutdown_callback()
    return {"status": "shutting_down"}


@app.get("/metrics")
async def metrics():
    """Return hardware metrics for this node."""
    mem = psutil.virtual_memory()
    uid = os.getuid()

    lean_procs = []
    for proc in psutil.process_iter(["pid", "name", "uids"]):
        try:
            if proc.info["uids"].real != uid:
                continue
            name = proc.info["name"]
            if name not in _LEAN_PROCESS_NAMES:
                continue
            with proc.oneshot():
                lean_procs.append(
                    {
                        "pid": proc.pid,
                        "name": name,
                        "rss_mb": round(proc.memory_info().rss / (1024 * 1024)),
                        "cpu_percent": proc.cpu_percent(),
                    }
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # CPU: per-core usage, then compute allocated vs total utilization
    allocated_cores = sorted(os.sched_getaffinity(0))
    total_cpus = psutil.cpu_count()
    per_cpu = psutil.cpu_percent(percpu=True)
    if allocated_cores and per_cpu:
        allocated_usage = [per_cpu[i] for i in allocated_cores if i < len(per_cpu)]
        cpu_percent_allocated = round(sum(allocated_usage) / len(allocated_usage), 1) if allocated_usage else 0.0
    else:
        cpu_percent_allocated = 0.0

    # Memory: use cgroup-aware values when available via SLURM
    slurm_mem_mb = os.environ.get("SLURM_MEM_PER_NODE")
    if slurm_mem_mb:
        allocated_mem_gb = round(int(slurm_mem_mb) / 1024, 1)
    else:
        allocated_mem_gb = round(mem.total / (1024**3), 1)

    # Per-user RSS total (what *we* are using, not other users)
    user_rss_bytes = 0
    for proc in psutil.process_iter(["uids"]):
        try:
            if proc.info["uids"].real == uid:
                user_rss_bytes += proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    user_mem_gb = round(user_rss_bytes / (1024**3), 1)

    return {
        "hostname": socket.gethostname(),
        "rank": _rank,
        "cpu_percent": cpu_percent_allocated,
        "cpu_percent_total": psutil.cpu_percent(),
        "cpu_count_allocated": len(allocated_cores),
        "cpu_count_total": total_cpus,
        "memory_allocated_gb": allocated_mem_gb,
        "memory_total_gb": round(mem.total / (1024**3), 1),
        "memory_used_gb": round(mem.used / (1024**3), 1),
        "memory_user_gb": user_mem_gb,
        "memory_percent": round(user_mem_gb / allocated_mem_gb * 100, 1) if allocated_mem_gb > 0 else 0.0,
        "memory_percent_total": mem.percent,
        "lean_processes": lean_procs,
        "uptime_s": round(time.monotonic() - _start_time, 1),
    }


def start_control_server(
    shutdown_callback: Callable[[], None] | None = None,
    *,
    rank: int = 0,
    port: int | None = None,
) -> int:
    """Start the control plane API in a background thread.

    Args:
        shutdown_callback: Called when POST /shutdown is received.
            If None, the /shutdown endpoint returns an error.
        rank: Rank number for this node (embedded in /metrics responses).
        port: Fixed port to bind to. If None, an available port is chosen.

    Returns:
        The port the server is listening on.
    """
    import uvicorn

    global _shutdown_callback, _rank, _start_time
    _shutdown_callback = shutdown_callback
    _rank = rank
    _start_time = time.monotonic()

    if port is None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            port = s.getsockname()[1]

    def serve():
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    logger.info("Control plane API running at http://localhost:%d", port)
    return port
