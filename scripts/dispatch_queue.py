#!/usr/bin/env python3
"""Queue/feed mechanics for ``/autoform:orchestrate`` — the bridge between the DAG
review dashboard's ``task_queue.json`` and the autoform run/review pipelines.

Pure, deterministic file mechanics with **zero model tokens**: read the next queued
task, flip its status (``queued`` -> ``running`` -> ``done``/``failed``/``parked``), and keep
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
  dispatch_queue.py <project> park  <id> [--reason R]
  dispatch_queue.py <project> resume <id>
  dispatch_queue.py <project> idle                 # reset the feed to idle
  dispatch_queue.py <project> status               # one line per task (banners all orchestrator-owned work)
  dispatch_queue.py <project> escalations          # proof recoveries + full failure notes
  dispatch_queue.py <project> mine                 # ALL open orchestrator-owned tasks — your full worklist
  dispatch_queue.py <project> orchestrator --state working --phase P [--detail D]   # set the live-feed orchestrator line
  dispatch_queue.py <project> agent-start --role R --name N [--target T] [--target-label L]  # show a subagent live
  dispatch_queue.py <project> agent-done --name N  # clear a subagent from the live feed

``enqueue`` lets the orchestrator (Claude, or any caller) add its OWN tasks to the
same queue the dashboard writes — so autonomous and human-dropped work share one
pipeline. It is idempotent: a duplicate (same agent+node already queued/running/parked)
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
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE / "review_ui") not in sys.path:
    sys.path.insert(0, str(_HERE / "review_ui"))
import fslock  # noqa: E402  — the SHARED cross-process lock (dashboard + engine)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class QueueStateError(ValueError):
    """The durable queue is present but cannot be trusted."""


def _load_json(path: Path, default):
    """Best-effort JSON for the ephemeral activity feed only."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load_queue(path: Path) -> list[dict[str, Any]]:
    """Load and validate the durable queue, failing closed on corruption.

    A missing queue is a valid empty queue. A present malformed queue must never
    be treated as empty: doing so lets the next enqueue overwrite durable work.
    Task ids are opaque but must be non-empty and unique because every lifecycle
    mutation addresses exactly one id.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QueueStateError(f"cannot read valid queue JSON at {path}: {error}") from error
    if not isinstance(data, list):
        raise QueueStateError(f"{path}: queue root must be a JSON array")
    tasks: list[dict[str, Any]] = []
    ids: set[str] = set()
    active_keys: set[tuple[str, str]] = set()
    for index, task in enumerate(data):
        if not isinstance(task, dict):
            raise QueueStateError(f"{path}: queue entry {index} must be an object")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise QueueStateError(f"{path}: queue entry {index} has no non-empty string id")
        if task_id in ids:
            raise QueueStateError(f"{path}: duplicate task id {task_id!r}")
        ids.add(task_id)
        if task.get("status") in {"queued", "running", "parked"}:
            agent, node = task.get("agent"), task.get("node")
            if isinstance(agent, str) and isinstance(node, str):
                key = (agent, node)
                if key in active_keys:
                    raise QueueStateError(
                        f"{path}: duplicate active task for {agent!r} -> {node!r}"
                    )
                active_keys.add(key)
        tasks.append(task)
    return tasks


def new_task_id(agent: str, node: str, tasks: list[dict[str, Any]]) -> str:
    """Return a deterministic id that remains unique across re-enqueues."""
    base = f"{agent}:{node}"
    existing = {str(task.get("id")) for task in tasks}
    if base not in existing:
        return base
    suffix = 2
    while f"{base}:{suffix}" in existing:
        suffix += 1
    return f"{base}:{suffix}"


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


def _feed_for(tasks: list, existing: dict | None = None) -> dict:
    """The dashboard live feed reflecting exactly the tasks currently ``running`` —
    never a fabricated ``running``: queue agents are derived from real queue
    state. Native host subagents have their own lifecycle and are preserved
    across queue synchronization when marked ``managed_by: native``."""
    running = [t for t in tasks if t.get("status") == "running"]
    existing = existing if isinstance(existing, dict) else {}
    native_agents = [
        dict(agent)
        for agent in existing.get("agents", [])
        if isinstance(agent, dict) and agent.get("managed_by") == "native"
    ] if isinstance(existing.get("agents"), list) else []
    queue_agents = [{
        "role": t.get("agent", "agent"),
        "name": t.get("id") or t.get("agent", "agent"),
        "target": t.get("node"),
        "target_label": t.get("node_label", t.get("node")),
        "status": "running",
        "detail": t.get("detail", ""),
        "managed_by": "queue",
    } for t in running]
    agents = native_agents + queue_agents
    existing_orchestrator = existing.get("orchestrator")
    native_orchestrator: dict = {}
    if isinstance(existing_orchestrator, dict):
        if existing_orchestrator.get("managed_by") == "native":
            native_orchestrator = dict(existing_orchestrator)
        elif isinstance(existing_orchestrator.get("native_orchestrator"), dict):
            native_orchestrator = dict(existing_orchestrator["native_orchestrator"])
    if running:
        queue_detail = "; ".join(
            f'{t.get("agent")} → {t.get("node")}' for t in running
        )
        native_detail = str(native_orchestrator.get("detail") or "").strip()
        detail = "; ".join(part for part in (native_detail, queue_detail) if part)
        orchestrator = {
            "state": "working",
            "phase": "dispatch",
            "detail": detail,
            "managed_by": "queue",
        }
        if native_orchestrator:
            orchestrator["native_orchestrator"] = native_orchestrator
    elif native_agents or native_orchestrator.get("state") not in (None, "idle"):
        orchestrator = native_orchestrator or {
            "state": "working",
            "managed_by": "native",
        }
    else:
        orchestrator = {"state": "idle"}
    return {"orchestrator": orchestrator, "agents": agents}


def sync_feed(path: Path, tasks: list) -> None:
    """Atomically synchronize queue workers without erasing native subagents."""
    with fslock.locked(path):
        existing = _load_json(path, {"orchestrator": {"state": "idle"}, "agents": []})
        _save(path, _feed_for(tasks, existing if isinstance(existing, dict) else None))


def _open_escalations(tasks: list) -> list:
    """Queued/running/parked proof-recovery tasks.

    These look like any other ``queued`` task but the deterministic engine NEVER
    drains them (it only drains ``reviewer``/``worker``); only ``/autoform:orchestrate``
    resolves them. Surfacing them distinctly is what stops the orchestrator from
    waiting on a queued escalation as if the engine would clear it (it never will)."""
    return [t for t in tasks
            if t.get("agent") == "escalation"
            and t.get("status") in ("queued", "running", "parked")]


# The queue has two consumers. The deterministic engine drains exactly these:
_ENGINE_KINDS = ("reviewer", "worker")


def _orch_kinds() -> tuple:
    """Kinds the orchestrator (or the worker CLI's agent stage) owns.

    Derived from the agent-role REGISTRY — every ``agents/<role>.md`` (and any
    project-local ``.autoform/agents/<role>.md``) that declares an agent-drained
    kind is accepted here, so adding a role file is all it takes to add a queue
    kind. Falls back to the historical built-in set if the registry is
    unavailable (e.g. a partial checkout).
    """
    try:
        sys.path.insert(0, str(_HERE.parent))
        from autoform_worker.registry import agent_kinds  # noqa: PLC0415

        discovered = agent_kinds(_HERE.parent)
        if discovered:
            return discovered
    except Exception:
        pass
    return ("escalation", "planner", "graphreview", "contentreview", "holistic", "mathcheck")


# ...and the orchestrator owns ALL the rest — each via the same lifecycle:
# claim -> run its Task subagent(s)/pipeline -> done (or fail). The engine NEVER
# closes an orchestrator-owned task, so one left queued sits forever until the
# orchestrator clears it. (Escalation was just the first symptom of this class.)
_ORCH_KINDS = _orch_kinds()


def _open_orchestrator_tasks(tasks: list) -> list:
    """Open (queued/running/parked) tasks the orchestrator owns — its full worklist.
    escalations. None are engine-drained; each needs claim -> run -> done. Escalations
    are the subset the engine auto-raises (and that carry the worker's words in ``note``)."""
    return [t for t in tasks
            if t.get("agent") in _ORCH_KINDS
            and t.get("status") in ("queued", "running", "parked")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Drain/sync the review dashboard queue.")
    ap.add_argument("project", type=Path, help="review project dir (holds task_queue.json)")
    ap.add_argument("cmd", choices=["next", "claim", "done", "fail", "park", "resume", "idle", "status", "enqueue",
                                    "escalations", "mine", "orchestrator", "agent-start", "agent-done"])
    ap.add_argument("id", nargs="?", help="task id (for claim/done/fail)")
    ap.add_argument("--detail", default="")
    ap.add_argument("--result", default="")
    ap.add_argument("--report-file", type=Path, default=None,
                    help="done: persist the agent's textual report on the queue task")
    ap.add_argument("--reason", default="")
    ap.add_argument("--fingerprint", default="", help="park: durable prover-input fingerprint after recovery")
    ap.add_argument("--agent", default="", help="enqueue: agent id — any palette kind (reviewer|worker|planner|graphreview|contentreview|holistic|mathcheck|escalation)")
    ap.add_argument("--node", default="", help="enqueue: target node id")
    ap.add_argument("--node-label", default="", help="enqueue: display label (defaults to --node)")
    ap.add_argument("--note", default="", help="enqueue: free-text payload (e.g. a worker's escalation reason)")
    ap.add_argument("--source", default="orchestrator", help="enqueue: who raised it (orchestrator|engine|human)")
    ap.add_argument("--role", default="", help="agent-start: subagent role (splitter|mathcheck|graphreview|contentreview|holistic|sourcesearch|…)")
    ap.add_argument("--name", default="", help="agent-start/agent-done: unique live-feed entry name (e.g. 'splitter:Covering spaces')")
    ap.add_argument("--target", default="", help="agent-start: node/cluster the subagent is working on")
    ap.add_argument("--target-label", default="", help="agent-start: display label for the target (defaults to --target)")
    ap.add_argument("--state", default="", help="orchestrator: state (working|idle|…; default working)")
    ap.add_argument("--phase", default="", help="orchestrator: phase label (e.g. 'Phase 2: splitting')")
    ap.add_argument("--stage", default="", choices=["", "setup", "plan", "approve", "split",
                                                    "prove", "publish"],
                    help="orchestrator: pipeline position (fixed vocabulary — drives the "
                         "dashboard stepper so users always know where they are)")
    a = ap.parse_args(argv)

    qp = a.project / "task_queue.json"
    fp = a.project / "agents_status.json"
    if a.cmd in {"idle", "orchestrator", "agent-start", "agent-done"}:
        tasks: list[dict[str, Any]] = []
    else:
        try:
            tasks = load_queue(qp)
        except QueueStateError as error:
            print(f"queue state error: {error}", file=sys.stderr)
            return 2

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
            print('      → worklist:  dispatch_queue.py <project> mine'
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
            print("  (no proof recoveries)")
            return 0
        print(f"  {len(open_esc)} proof-recovery task(s) — these are YOURS, not the engine's:\n")
        for t in open_esc:
            print(f'  • {t.get("node","?")}   [{t.get("status","?")}]   id={t.get("id","?")}')
            for line in str(t.get("note", "")).strip().splitlines():
                print(f'      {line}')
            print()
        print("  Run the ordered recovery waves, then mark each done for a retry or parked")
        print("  with its evidence ledger. A parked recovery may be resumed later.")
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
        print("\n  Finish actionable work; parked recoveries remain visible for later evidence.")
        return 0
    if a.cmd == "idle":
        with fslock.locked(fp):
            _save(fp, {"orchestrator": {"state": "idle"}, "agents": []})
        print("feed idle")
        return 0
    if a.cmd == "enqueue":
        if not (a.agent and a.node):
            ap.error("enqueue needs --agent and --node")
        with fslock.locked(qp):                      # cross-process: dashboard writes too
            try:
                tasks = load_queue(qp)
            except QueueStateError as error:
                print(f"queue state error: {error}", file=sys.stderr)
                return 2
            if any(t.get("status") in ("queued", "running", "parked") and t.get("agent") == a.agent
                   and t.get("node") == a.node for t in tasks):
                print(f"already queued/running/parked: {a.agent} -> {a.node} (skipped)")
                return 0
            tid = new_task_id(a.agent, a.node, tasks)
            entry = {"id": tid, "agent": a.agent, "node": a.node,
                     "node_label": a.node_label or a.node, "status": "queued",
                     "at": _now(), "source": a.source or "orchestrator"}
            if a.note:
                entry["note"] = a.note
            tasks.append(entry)
            _save(qp, tasks)
            sync_feed(fp, tasks)
        print(f"enqueued {tid}")
        return 0

    # --- live-feed writers (agents_status.json directly; NOT queue-derived) --------
    # Let the `plan` orchestrator surface its Task subagents in the dashboard during
    # setup, when no engine runs to sync the feed from the queue. Each preserves the
    # other entries (keyed by `name`) under the feed's own lock, so parallel splitters
    # coexist. Handled before the id-required branch since none take a task id.
    _DEFAULT_FEED = {"orchestrator": {"state": "idle"}, "agents": []}
    if a.cmd in ("orchestrator", "agent-start", "agent-done"):
        with fslock.locked(fp):
            feed = _load_json(fp, dict(_DEFAULT_FEED))
            if not isinstance(feed, dict):
                feed = {"orchestrator": {"state": "idle"}, "agents": []}
            feed.setdefault("agents", [])
            if not isinstance(feed.get("agents"), list):
                feed["agents"] = []
            if a.cmd == "orchestrator":
                orch = {"state": a.state or "working", "managed_by": "native"}
                if a.phase:
                    orch["phase"] = a.phase
                if a.detail:
                    orch["detail"] = a.detail
                if a.stage:
                    orch["stage"] = a.stage
                feed["orchestrator"] = orch
                msg = f"orchestrator -> {orch['state']}" + (f" · {a.phase}" if a.phase else "")
            elif a.cmd == "agent-start":
                if not (a.role and a.name):
                    ap.error("agent-start needs --role and --name")
                feed["agents"] = [x for x in feed["agents"]
                                  if not (isinstance(x, dict) and x.get("name") == a.name)]
                feed["agents"].append({
                    "role": a.role, "name": a.name,
                    "target": a.target, "target_label": a.target_label or a.target,
                    "status": "running", "detail": a.detail,
                    "managed_by": "native",
                })
                orch = feed.get("orchestrator") or {}
                if not isinstance(orch, dict) or orch.get("state", "idle") == "idle":
                    orch = dict(orch) if isinstance(orch, dict) else {}
                    orch["state"] = "working"
                    orch["managed_by"] = "native"
                    feed["orchestrator"] = orch
                msg = f"agent-start {a.name}" + (f" -> {a.target}" if a.target else "")
            else:  # agent-done
                if not a.name:
                    ap.error("agent-done needs --name")
                feed["agents"] = [x for x in feed["agents"]
                                  if not (isinstance(x, dict) and x.get("name") == a.name)]
                msg = f"agent-done {a.name}"
            _save(fp, feed)
        print(msg)
        return 0

    if not a.id:
        ap.error(f"{a.cmd} needs a task id")
    with fslock.locked(qp):                          # cross-process: dashboard writes too
        try:
            tasks = load_queue(qp)
        except QueueStateError as error:
            print(f"queue state error: {error}", file=sys.stderr)
            return 2
        t = next((t for t in tasks if t.get("id") == a.id), None)
        if t is None:
            print(f"no task {a.id!r} in {qp}", file=sys.stderr)
            return 1
        current = t.get("status")
        if a.cmd == "claim":
            if current == "running":
                print(f"claim {a.id} refused: task is already running", file=sys.stderr)
                return 1
            if current != "queued":
                print(
                    f"invalid transition for {a.id}: {current!r} -> running",
                    file=sys.stderr,
                )
                return 1
            t["status"] = "running"
            t["started_at"] = _now()
            if a.detail:
                t["detail"] = a.detail
        elif a.cmd == "done":
            if current == "done":
                print(f"done {a.id} -> done (already finished)")
                return 0
            if current != "running":
                print(
                    f"invalid transition for {a.id}: {current!r} -> done",
                    file=sys.stderr,
                )
                return 1
            t["status"] = "done"
            t["finished_at"] = _now()
            if a.result:
                t["result"] = a.result
            if a.report_file is not None:
                try:
                    report = a.report_file.read_text(encoding="utf-8", errors="replace")
                except OSError as error:
                    print(f"cannot read report file: {error}", file=sys.stderr)
                    return 1
                t["report"] = report[-40_000:]
        elif a.cmd == "fail":
            if current == "failed":
                print(f"fail {a.id} -> failed (already finished)")
                return 0
            if current != "running":
                print(
                    f"invalid transition for {a.id}: {current!r} -> failed",
                    file=sys.stderr,
                )
                return 1
            t["status"] = "failed"
            t["finished_at"] = _now()
            if a.reason:
                t["result"] = a.reason
            if a.report_file is not None and a.report_file.is_file():
                t["report"] = a.report_file.read_text(
                    encoding="utf-8", errors="replace")[-40_000:]
        elif a.cmd == "park":
            if current == "parked":
                print(f"park {a.id} -> parked (already parked)")
                return 0
            if current != "running":
                print(
                    f"invalid transition for {a.id}: {current!r} -> parked",
                    file=sys.stderr,
                )
                return 1
            t["status"] = "parked"
            t["finished_at"] = _now()
            if a.reason:
                t["result"] = a.reason
            if a.report_file is not None and a.report_file.is_file():
                t["report"] = a.report_file.read_text(
                    encoding="utf-8", errors="replace")[-40_000:]
            if isinstance(t.get("recovery"), dict):
                t["recovery"]["phase"] = "parked"
                if a.fingerprint:
                    t["recovery"]["fingerprint"] = a.fingerprint
        elif a.cmd == "resume":
            if current == "queued":
                print(f"resume {a.id} -> queued (already queued)")
                return 0
            if current != "parked":
                print(
                    f"invalid transition for {a.id}: {current!r} -> queued",
                    file=sys.stderr,
                )
                return 1
            t["status"] = "queued"
            t["at"] = _now()
            t.pop("started_at", None)
            t.pop("finished_at", None)
            if isinstance(t.get("recovery"), dict):
                t["recovery"]["phase"] = "proof-research"
        _save(qp, tasks)
        sync_feed(fp, tasks)
    print(f'{a.cmd} {a.id} -> {t["status"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
