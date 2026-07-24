#!/usr/bin/env python3
"""Queue/feed mechanics for ``/autoform:orchestrate`` — the bridge between the DAG
review dashboard's ``task_queue.json`` and the autoform run/review pipelines.

Pure, deterministic file mechanics with **zero model tokens**: read the next queued
task, flip its status (``queued`` -> ``running`` -> ``done``/``failed``), and keep
``agents_status.json`` (the dashboard's live feed) in sync so a drop in the UI shows
the agent *working* while the session does the real work. The privileged agent work
(prove / review) is the command's job — this only moves the paperwork, atomically.

The dashboard contract (both files sit next to ``graph.json`` in the review project):
  * ``task_queue.json`` = ``[{id, agent, node, node_label, status, at, source, note?, ...}]``
    — ``source`` (``orchestrator``|``engine``|``human``, default ``orchestrator``) is who
    raised the task; ``note`` is an optional free-text payload (e.g. a worker's escalation
    prose). ``status`` shows a one-line ``note`` preview; the full text stays in the file.
  * ``agents_status.json`` = ``{orchestrator:{state,phase,detail}, agents:[{role,name,
    target,target_label,status,detail}]}`` — exactly what serve_review reads.

Usage::

  dispatch_queue.py <project> next                 # next queued task as JSON ('' if none)
  dispatch_queue.py <project> enqueue --agent A --node N [--node-label L] [--note T] [--source S]
  dispatch_queue.py <project> claim <id> [--detail D]
  dispatch_queue.py <project> done  <id> [--result R]
  dispatch_queue.py <project> fail  <id> [--reason R]
  dispatch_queue.py <project> idle                 # reset the feed to idle
  dispatch_queue.py <project> status               # one line per task (banners all orchestrator-owned work)
  dispatch_queue.py <project> escalations          # open escalations + full notes (engine-raised walls)
  dispatch_queue.py <project> mine                 # ALL open orchestrator-owned tasks — your full worklist

``enqueue`` lets the orchestrator (Claude, or any caller) add its OWN tasks to the
same queue the dashboard writes — so autonomous and human-dropped work share one
pipeline. It is idempotent: a duplicate (same agent+node already queued/running)
is skipped, never double-queued.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE / "review_ui") not in sys.path:
    sys.path.insert(0, str(_HERE / "review_ui"))
import fslock  # noqa: E402  — the SHARED cross-process lock (dashboard + engine)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save(path: Path, data) -> None:
    """Atomic write (unique mkstemp temp + os.replace) — never leave a half-written
    queue/feed, and never share a fixed temp name two writers could tear. Callers
    doing load-mutate-save on a shared file hold ``fslock.locked(path)`` around the
    whole cycle; this alone only makes the single write atomic."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def _open_escalations(tasks: list) -> list:
    """Queued/running ``escalation`` tasks — the orchestrator's triage worklist.

    These look like any other ``queued`` task but the deterministic engine NEVER
    drains them (it only drains ``reviewer``/``worker``); only ``/autoform:orchestrate``
    resolves them. Surfacing them distinctly is what stops the orchestrator from
    waiting on a queued escalation as if the engine would clear it (it never will)."""
    return [t for t in tasks
            if t.get("agent") == "escalation" and t.get("status") in ("queued", "running")]


# The queue has two consumers. The deterministic engine drains exactly these:
_ENGINE_KINDS = ("reviewer", "worker")
# ...and the orchestrator (/autoform:orchestrate) owns ALL the rest — each via the same
# lifecycle: claim -> run its Task subagent(s)/pipeline -> done (or fail). The engine
# NEVER closes an orchestrator-owned task, so one left queued sits forever until the
# orchestrator clears it. (Escalation was just the first symptom of this whole class.)
_ORCH_KINDS = ("escalation", "planner", "graphreview", "contentreview", "holistic", "mathcheck")


def _open_orchestrator_tasks(tasks: list) -> list:
    """Open (queued/running) tasks the orchestrator owns — its FULL worklist, not just
    escalations. None are engine-drained; each needs claim -> run -> done. Escalations
    are the subset the engine auto-raises (and that carry the worker's words in ``note``)."""
    return [t for t in tasks
            if t.get("agent") in _ORCH_KINDS and t.get("status") in ("queued", "running")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Drain/sync the review dashboard queue.")
    ap.add_argument("project", type=Path, help="review project dir (holds task_queue.json)")
    ap.add_argument("cmd", choices=["next", "claim", "done", "fail", "idle", "status", "enqueue", "escalations", "mine"])
    ap.add_argument("id", nargs="?", help="task id (for claim/done/fail)")
    ap.add_argument("--detail", default="")
    ap.add_argument("--result", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--agent", default="", help="enqueue: agent id — any palette kind (reviewer|worker|planner|graphreview|contentreview|holistic|mathcheck|escalation)")
    ap.add_argument("--node", default="", help="enqueue: target node id")
    ap.add_argument("--node-label", default="", help="enqueue: display label (defaults to --node)")
    ap.add_argument("--note", default="", help="enqueue: free-text payload (e.g. a worker's escalation reason)")
    ap.add_argument("--source", default="orchestrator", help="enqueue: who raised it (orchestrator|engine|human)")
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
            return 0
        open_orch = _open_orchestrator_tasks(tasks)
        if open_orch:                               # impossible to miss in a poll
            tally: dict = {}
            for t in open_orch:
                tally[t.get("agent", "?")] = tally.get(t.get("agent", "?"), 0) + 1
            breakdown = ", ".join(f"{n}×{k}" for k, n in sorted(tally.items()))
            print(f'  ⚑⚑ {len(open_orch)} TASK(S) AWAIT THE ORCHESTRATOR — the engine drains NONE of these;')
            print(f'      each needs claim → run → done:  {breakdown}')
            print(f'      → worklist:  dispatch_queue.py <project> mine'
                  + ('   ·  escalations carry a worker’s words' if tally.get("escalation") else ''))
            print()
        for t in tasks:
            print(f'  {t.get("status","?"):8} {t.get("agent","?"):9} {t.get("node","?")}')
            if t.get("note"):                       # preview (full text lives in task_queue.json)
                note = " ".join(str(t["note"]).split())
                print(f'           ↳ note: {note[:160]}{"…" if len(note) > 160 else ""}')
        return 0
    if a.cmd == "escalations":
        open_esc = _open_escalations(tasks)
        if not open_esc:
            print("  (no open escalations)")
            return 0
        print(f"  {len(open_esc)} open escalation(s) awaiting triage — these are YOURS, not the engine's:\n")
        for t in open_esc:
            print(f'  • {t.get("node","?")}   [{t.get("status","?")}]   id={t.get("id","?")}')
            for line in str(t.get("note", "")).strip().splitlines():
                print(f'      {line}')
            print()
        print("  Triage each (claim → grow the DAG via merge_node.py / run planner / surface to the")
        print("  user → done), then the blocked node can be re-queued. Never leave one queued.")
        return 0
    if a.cmd == "mine":
        open_orch = _open_orchestrator_tasks(tasks)
        if not open_orch:
            print("  (nothing awaiting the orchestrator — reviewer/worker tasks drain themselves)")
            return 0
        order = {k: i for i, k in enumerate(_ORCH_KINDS)}        # escalations first
        print(f"  {len(open_orch)} task(s) awaiting the orchestrator — the engine drains NONE of these.")
        print("  Each: claim → run its Task subagent(s)/pipeline (graph edits via merge_node.py) → done.\n")
        for t in sorted(open_orch, key=lambda x: (order.get(x.get("agent"), 99), str(x.get("node", "")))):
            print(f'  • {t.get("agent","?"):13} {t.get("node","?")}   [{t.get("status","?")}]   id={t.get("id","?")}')
            if t.get("agent") == "escalation" and t.get("note"):  # the worker's own words
                for line in str(t["note"]).strip().splitlines():
                    print(f'        {line}')
        print("\n  Never leave one queued/running when a run ends (orchestrate.md step 5).")
        return 0
    if a.cmd == "idle":
        _save(fp, {"orchestrator": {"state": "idle"}, "agents": []})
        print("feed idle")
        return 0
    if a.cmd == "enqueue":
        if not (a.agent and a.node):
            ap.error("enqueue needs --agent and --node")
        with fslock.locked(qp):                      # cross-process: dashboard writes too
            tasks = _load(qp, [])
            if not isinstance(tasks, list):
                tasks = []
            if any(t.get("status") in ("queued", "running") and t.get("agent") == a.agent
                   and t.get("node") == a.node for t in tasks):
                print(f"already queued/running: {a.agent} -> {a.node} (skipped)")
                return 0
            tid = f"{a.agent}-{a.node}-{_now().replace(':', '').replace('-', '')}"
            entry = {"id": tid, "agent": a.agent, "node": a.node,
                     "node_label": a.node_label or a.node, "status": "queued",
                     "at": _now(), "source": a.source or "orchestrator"}
            if a.note:
                entry["note"] = a.note
            tasks.append(entry)
            _save(qp, tasks)
            _save(fp, _feed_for(tasks))
        print(f"enqueued {tid}")
        return 0

    if not a.id:
        ap.error(f"{a.cmd} needs a task id")
    with fslock.locked(qp):                          # cross-process: dashboard writes too
        tasks = _load(qp, [])
        if not isinstance(tasks, list):
            tasks = []
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
