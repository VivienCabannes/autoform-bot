#!/usr/bin/env python3
"""Queue/feed mechanics for ``/autoform:dispatch`` — the bridge between the DAG
review dashboard's ``task_queue.json`` and the autoform run/review pipelines.

Pure, deterministic file mechanics with **zero model tokens**: read the next queued
task, flip its status (``queued`` -> ``running`` -> ``done``/``failed``), and keep
``agents_status.json`` (the dashboard's live feed) in sync so a drop in the UI shows
the agent *working* while the session does the real work. The privileged agent work
(prove / review) is the command's job — this only moves the paperwork, atomically.

The dashboard contract (both files sit next to ``graph.json`` in the review project):
  * ``task_queue.json`` = ``[{id, agent, node, node_label, status, at, ...}]``
  * ``agents_status.json`` = ``{orchestrator:{state,phase,detail}, agents:[{role,name,
    target,target_label,status,detail}]}`` — exactly what serve_review reads.

Usage::

  dispatch_queue.py <project> next                 # next queued task as JSON ('' if none)
  dispatch_queue.py <project> claim <id> [--detail D]
  dispatch_queue.py <project> done  <id> [--result R]
  dispatch_queue.py <project> fail  <id> [--reason R]
  dispatch_queue.py <project> idle                 # reset the feed to idle
  dispatch_queue.py <project> status               # one line per task
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save(path: Path, data) -> None:
    """Atomic write (temp + os.replace) — never leave a half-written queue/feed."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _feed_for(tasks: list) -> dict:
    """The dashboard live feed reflecting exactly the tasks currently ``running`` —
    never a fabricated ``running``: it is derived from real queue state."""
    running = [t for t in tasks if t.get("status") == "running"]
    if not running:
        return {"orchestrator": {"state": "idle"}, "agents": []}
    agents = [{
        "role": t.get("agent", "agent"),
        "name": t.get("agent", "agent"),
        "target": t.get("node"),
        "target_label": t.get("node_label", t.get("node")),
        "status": "running",
        "detail": t.get("detail", ""),
    } for t in running]
    detail = "; ".join(f'{t.get("agent")} → {t.get("node")}' for t in running)
    return {"orchestrator": {"state": "working", "phase": "dispatch", "detail": detail},
            "agents": agents}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Drain/sync the review dashboard queue.")
    ap.add_argument("project", type=Path, help="review project dir (holds task_queue.json)")
    ap.add_argument("cmd", choices=["next", "claim", "done", "fail", "idle", "status"])
    ap.add_argument("id", nargs="?", help="task id (for claim/done/fail)")
    ap.add_argument("--detail", default="")
    ap.add_argument("--result", default="")
    ap.add_argument("--reason", default="")
    a = ap.parse_args(argv)

    qp = a.project / "task_queue.json"
    fp = a.project / "agents_status.json"
    tasks = _load(qp, [])
    if not isinstance(tasks, list):
        tasks = []

    if a.cmd == "next":
        nxt = next((t for t in tasks if t.get("status") == "queued"), None)
        print(json.dumps(nxt) if nxt else "")
        return 0
    if a.cmd == "status":
        if not tasks:
            print("  (queue empty)")
        for t in tasks:
            print(f'  {t.get("status","?"):8} {t.get("agent","?"):9} {t.get("node","?")}')
        return 0
    if a.cmd == "idle":
        _save(fp, {"orchestrator": {"state": "idle"}, "agents": []})
        print("feed idle")
        return 0

    if not a.id:
        ap.error(f"{a.cmd} needs a task id")
    t = next((t for t in tasks if t.get("id") == a.id), None)
    if t is None:
        print(f"no task {a.id!r} in {qp}", file=sys.stderr)
        return 1
    if a.cmd == "claim":
        t["status"] = "running"
        t["started_at"] = _now()
        if a.detail:
            t["detail"] = a.detail
    elif a.cmd == "done":
        t["status"] = "done"
        t["finished_at"] = _now()
        if a.result:
            t["result"] = a.result
    elif a.cmd == "fail":
        t["status"] = "failed"
        t["finished_at"] = _now()
        if a.reason:
            t["result"] = a.reason
    _save(qp, tasks)
    _save(fp, _feed_for(tasks))
    print(f'{a.cmd} {a.id} -> {t["status"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
