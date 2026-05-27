# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Hub gateway for Autoform Visualizer — per-run process isolation.

Discovers runs in a directory, spawns an isolated visualizer process per run,
and proxies all requests through a single port.

Run with:
    python -m autoform.visualizer.hub --runs-dir /path/to/runs

Then open: http://localhost:8001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

WEBAPP_DIR = Path(__file__).parent
TEMPLATES_DIR = WEBAPP_DIR / "templates"

hub = FastAPI(title="Autoform Visualizer Hub")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Formatting filters (shared with app.py) ──────────────────────


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
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_duration(s: float | None) -> str:
    if not s:
        return "—"
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {sec}s"


templates.env.filters["fmt_cost"] = fmt_cost
templates.env.filters["fmt_tokens"] = fmt_tokens
templates.env.filters["fmt_duration"] = fmt_duration


# ── Per-run process management ────────────────────────────────────


@dataclass
class RunServer:
    name: str
    port: int
    process: subprocess.Popen


@dataclass
class HubState:
    runs_dir: Path = field(default_factory=Path)
    servers: dict[str, RunServer] = field(default_factory=dict)
    client: httpx.AsyncClient | None = None

    def discover_runs(self) -> list[str]:
        """Find subdirectories that look like autoform runs."""
        if not self.runs_dir.exists():
            return []
        return sorted(
            [
                d.name
                for d in self.runs_dir.iterdir()
                if d.is_dir() and ((d / "dag.json").exists() or (d / "traces").is_dir())
            ],
            reverse=True,
        )

    def _allocate_port(self) -> int:
        """Get an available ephemeral port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    def spawn_server(self, run_name: str) -> RunServer:
        """Spawn a per-run visualizer subprocess."""
        port = self._allocate_port()
        env = {
            **os.environ,
            "VIZV1_RUNS_DIR": str(self.runs_dir),
            "VIZV1_RUN_FILTER": run_name,
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "autoform.visualizer.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
                "--log-level",
                "warning",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        server = RunServer(name=run_name, port=port, process=process)
        self.servers[run_name] = server
        return server

    def ensure_server(self, run_name: str) -> RunServer:
        """Get or create a server for the given run."""
        existing = self.servers.get(run_name)
        if existing and existing.process.poll() is None:
            return existing
        # Server doesn't exist or has died — (re)spawn
        if existing:
            del self.servers[run_name]
        return self.spawn_server(run_name)

    def stop_all(self):
        """Terminate all child processes."""
        for server in self.servers.values():
            if server.process.poll() is None:
                server.process.terminate()
        for server in self.servers.values():
            try:
                server.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.process.kill()
        self.servers.clear()


_state = HubState()

_STARTUP_TIMEOUT = 10.0
_STARTUP_POLL = 0.1


async def _wait_for_server(server: RunServer):
    """Wait until the per-run server is accepting connections."""
    deadline = _time.monotonic() + _STARTUP_TIMEOUT
    while _time.monotonic() < deadline:
        if server.process.poll() is not None:
            stderr = server.process.stderr.read().decode() if server.process.stderr else ""
            raise RuntimeError(f"Per-run server for '{server.name}' exited early: {stderr[:500]}")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.close()
            await writer.wait_closed()
            return
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(_STARTUP_POLL)
    raise RuntimeError(f"Per-run server for '{server.name}' did not start within {_STARTUP_TIMEOUT}s")


async def _get_client() -> httpx.AsyncClient:
    if _state.client is None or _state.client.is_closed:
        _state.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    return _state.client


# ── Run stats (lightweight, no per-run server needed) ─────────────


def _load_run_summary(run_dir: Path) -> dict:
    """Load minimal stats for the runs listing page."""
    dag_path = run_dir / "dag.json"
    if not dag_path.exists():
        return {
            "name": run_dir.name,
            "total_tasks": 0,
            "completed": 0,
            "failed": 0,
            "total_cost": 0,
            "total_tokens": 0,
            "duration": None,
            "loc": None,
        }
    try:
        with open(dag_path) as f:
            dag = json.load(f)
    except Exception:
        dag = {}

    items = dag.get("items", [])
    completed = sum(1 for t in items if t.get("status") == "completed")
    failed = sum(1 for t in items if t.get("status") == "failed")

    return {
        "name": run_dir.name,
        "total_tasks": len(items),
        "completed": completed,
        "failed": failed,
        "total_cost": 0,
        "total_tokens": 0,
        "duration": None,
        "loc": None,
    }


# ── Routes ────────────────────────────────────────────────────────


@hub.get("/", response_class=HTMLResponse)
async def list_runs(request: Request):
    """Runs listing page — served directly by the hub."""
    runs = _state.discover_runs()
    run_summaries = [_load_run_summary(_state.runs_dir / name) for name in runs]
    return templates.TemplateResponse(request, "runs.html", {"runs": run_summaries})


@hub.api_route("/run/{run_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_run(request: Request, run_name: str, path: str):
    """Proxy requests to the per-run visualizer server."""
    available = _state.discover_runs()
    if run_name not in available:
        raise HTTPException(404, f"Run '{run_name}' not found")

    server = _state.ensure_server(run_name)

    try:
        await _wait_for_server(server)
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    url = f"http://127.0.0.1:{server.port}/run/{run_name}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    client = await _get_client()
    body = await request.body()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "transfer-encoding")}

    try:
        upstream = await client.request(
            method=request.method,
            url=url,
            headers=fwd_headers,
            content=body if body else None,
        )
    except httpx.RequestError as e:
        raise HTTPException(502, f"Failed to reach per-run server: {e}")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
        },
        media_type=upstream.headers.get("content-type"),
    )


@hub.api_route("/run/{run_name}", methods=["GET"])
async def proxy_run_root(request: Request, run_name: str):
    """Proxy the run root page (no trailing path)."""
    return await proxy_run(request, run_name, "")


@hub.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_api(request: Request, path: str):
    """Proxy API calls — determine which run server to forward to from the referer."""
    referer = request.headers.get("referer", "")
    match = re.search(r"/run/([^/]+)", referer)
    if not match:
        raise HTTPException(400, "Cannot determine run from API request — no run context")
    run_name = match.group(1)

    available = _state.discover_runs()
    if run_name not in available:
        raise HTTPException(404, f"Run '{run_name}' not found")

    server = _state.ensure_server(run_name)
    try:
        await _wait_for_server(server)
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    url = f"http://127.0.0.1:{server.port}/api/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    client = await _get_client()
    body = await request.body()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "transfer-encoding")}

    # Check if this is an SSE endpoint
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept or path == "eval-log":
        # Use streaming for SSE
        req = client.build_request(
            method=request.method,
            url=url,
            headers=fwd_headers,
            content=body if body else None,
        )
        try:
            upstream = await client.send(req, stream=True)
        except httpx.RequestError as e:
            raise HTTPException(502, f"Failed to reach per-run server: {e}")

        async def stream():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    try:
        upstream = await client.request(
            method=request.method,
            url=url,
            headers=fwd_headers,
            content=body if body else None,
        )
    except httpx.RequestError as e:
        raise HTTPException(502, f"Failed to reach per-run server: {e}")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
        },
        media_type=upstream.headers.get("content-type"),
    )


# ── Lifecycle ─────────────────────────────────────────────────────


@hub.on_event("startup")
async def on_startup():
    """Pre-spawn servers for all discovered runs."""
    # Restore runs_dir from env (set by __main__, needed after uvicorn re-import)
    env_dir = os.environ.get("_HUB_RUNS_DIR")
    if env_dir and not _state.runs_dir.parts:
        _state.runs_dir = Path(env_dir)

    runs = _state.discover_runs()
    for run_name in runs:
        _state.spawn_server(run_name)
    if runs:
        print(f"Spawned {len(runs)} per-run server(s): {', '.join(runs)}")


@hub.on_event("shutdown")
async def on_shutdown():
    _state.stop_all()
    if _state.client and not _state.client.is_closed:
        await _state.client.aclose()


# ── Main ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Autoform Visualizer Hub")
    parser.add_argument("--runs-dir", type=Path, required=True, help="Directory containing run subdirectories")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    _state.runs_dir = args.runs_dir.expanduser().resolve()
    os.environ["_HUB_RUNS_DIR"] = str(_state.runs_dir)
    print(f"Hub runs directory: {_state.runs_dir}")

    # Handle graceful shutdown
    def _sighandler(signum, frame):
        _state.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    uvicorn.run(
        "autoform.visualizer.hub:hub",
        host=args.host,
        port=args.port,
        workers=1,
    )
