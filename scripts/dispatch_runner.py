#!/usr/bin/env python3
"""Deterministic parallel dispatcher for the DAG review dashboard.

Reads ``task_queue.json`` and fans work out through an explicitly selected jury
provider, with **no reliance on an LLM orchestrator choosing to delegate**. Each REVIEWER
task spawns the 3-judge jury (faithfulness / proof_integrity / code_quality)
concurrently; ALL queued nodes' judges run in one bounded process pool, so nodes
are reviewed **in parallel, not one-by-one**. The single parent process is the
only writer of ``review_status.json`` (atomic, under a lock) — no write race.

Claude judges scrub API credentials to use the logged-in subscription; Codex
uses its configured auth; API judges use the configured endpoint. Every judge
is read-only and the parent process is the sole verdict writer.

Usage::

  env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN python3 scripts/dispatch_runner.py <project-dir> \\
      [--repo <lean-repo>] [--jobs 9] [--judge-backend claude|codex|openai|avocado]
      [--model <provider-model>] [--limit N] [--dry-run]

``<project-dir>`` holds graph.json + task_queue.json + review_status.json.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import fcntl
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))   # plugin root, for prover/runtime imports
import fslock  # noqa: E402  # cross-process lock shared with serve_review
from visualization import review_model as rm  # noqa: E402  # sidecar / jury verdict
import dispatch_queue as dq  # noqa: E402  # _save / _feed_for / _now
import backend_config  # noqa: E402  # user-facing backend selection
import judge_runtime  # noqa: E402  # structured jury across CLI/API providers
import merge_node  # noqa: E402  # locked graph writer
import target_state  # noqa: E402  # proof/review freshness fingerprints
try:
    from autoform.prover.driver import prove as _prove
    from autoform.prover.claude_adapter import ClaudeAdapter as _ClaudeAdapter
    from autoform.prover.steerer import Steerer as _Steerer
    from autoform.prover.verify import verify_proof as _verify_existing_proof
    try:
        from autoform.prover.aristotle import build_node_spec as _build_node_spec
    except Exception:
        _build_node_spec = None
    _PROVER_OK, _PROVER_ERR = True, ""
except Exception as _e:                 # prover deps absent → --workers reports it cleanly
    _PROVER_OK, _PROVER_ERR, _build_node_spec, _verify_existing_proof = (
        False, str(_e), None, None
    )

# The jury axes + rubrics come from review_model — the SINGLE SOURCE OF TRUTH
# (internal/rubrics/*.json). Add/remove a rubric file and the jury here
# follows with no edit: AXES, the per-node judge fan-out, and the verdict all adapt.
AXES = rm.AXES
load_rubrics = rm.load_rubrics
_ACTIVE_JUDGE_BACKEND = "claude"


class DispatcherBusy(RuntimeError):
    """Another dispatcher owns this project."""


@contextlib.contextmanager
def dispatcher_lease(project: Path):
    """Hold one non-blocking process lease for the complete dispatcher run.

    Queue locks protect individual transactions; this lease protects the
    engine lifecycle. Without it, a second startup can mistake the first
    engine's live ``running`` tasks for crash leftovers and requeue them.
    """
    project = project.resolve()
    state_dir = project / ".autoform"
    if state_dir.is_symlink():
        raise ValueError(f"dispatcher state directory is a symlink: {state_dir}")
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "dispatcher.lock"
    if lock_path.is_symlink():
        raise ValueError(f"dispatcher lease is a symlink: {lock_path}")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock.seek(0)
            holder = lock.read().strip() or "unknown holder"
            raise DispatcherBusy(
                f"another dispatcher already owns {project} ({holder})"
            ) from error
        try:
            lock.seek(0)
            lock.truncate()
            json.dump({"pid": os.getpid(), "started_at": dq._now()}, lock)
            lock.write("\n")
            lock.flush()
            os.fsync(lock.fileno())
            yield lock_path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nearest_lean_root(project: Path) -> Path:
    """Nearest project/ancestor with a lakefile; the dispatch project otherwise."""
    project = project.resolve()
    for candidate in (project, *project.parents):
        if (candidate / "lakefile.toml").is_file() or (candidate / "lakefile.lean").is_file():
            return candidate
    return project


def _mark_proof_verified(
    graph_path: Path,
    project: Path,
    lean_root: Path,
    node_id: str,
    expected_spec_fingerprint: str,
) -> None:
    """Durably mark one explicit proof target after the shared kernel gate passes."""
    lock_path = Path(str(graph_path) + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        node = graph.get("nodes", {}).get(node_id)
        if not isinstance(node, dict):
            raise ValueError(f"cannot mark absent graph node {node_id!r} proved")
        current_spec = target_state.spec_fingerprint(project, node_id, node)
        if current_spec != expected_spec_fingerprint:
            raise ValueError("target specification changed while the proof worker was running")
        if node.get("spec_status") != "ready" or node.get("proof_status") != "pending":
            raise ValueError("target lifecycle changed while the proof worker was running")
        updated = dict(node)
        updated["proof_status"] = "proved"
        updated["proof_verified_at"] = _now()
        updated["proof_fingerprint"] = target_state.artifact_fingerprint(
            project, lean_root, node_id, node
        )
        merge_node.merge(str(graph_path), {"upsert": {node_id: updated}})


def _mark_proof_blocked(
    graph_path: Path,
    project: Path,
    node_id: str,
    expected_spec_fingerprint: str,
) -> None:
    """Make escalation exhaustion durable so the orchestrator cannot requeue forever."""
    lock_path = Path(str(graph_path) + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        node = graph.get("nodes", {}).get(node_id)
        if not isinstance(node, dict) or "proof_status" not in node:
            return
        if (
            node.get("proof_status") != "pending"
            or node.get("spec_status") != "ready"
            or target_state.spec_fingerprint(project, node_id, node)
            != expected_spec_fingerprint
        ):
            raise ValueError("target changed before escalation block could be recorded")
        updated = dict(node)
        updated["proof_status"] = "blocked"
        updated["proof_blocked_at"] = _now()
        merge_node.merge(str(graph_path), {"upsert": {node_id: updated}})


def _invalidate_ai_review(sidecar_path: Path, node_id: str) -> None:
    """Remove only the stale AI slot after a verified proof changes target code."""
    with fslock.locked(sidecar_path):
        sidecar = rm.load_sidecar(sidecar_path)
        review = sidecar.get("reviews", {}).get(node_id)
        if isinstance(review, dict) and "ai" in review:
            del review["ai"]
            rm.save_sidecar(sidecar_path, sidecar)


def build_prompt(rubric: dict, node_id: str, node: dict, content_text: str) -> str:
    """Fill the rubric's prompt_template from the node's graph data + prose."""
    crit = "\n".join(f"{k}: {v}" for k, v in rubric["criteria"].items())
    decls = ", ".join(node.get("lean_declarations") or node.get("mathlib_declarations") or []) \
        or "(the declaration names are listed in the node content below — find them in the repo)"
    loc = "; ".join(
        f'{r.get("file", "")}:{r.get("location", "")}' for r in (node.get("source_refs") or [])
    ) or node.get("mathlib_notes", "")
    return rubric["prompt_template"].format(
        name=node_id,
        kind=node.get("kind", "statement"),
        location=loc,
        description=content_text or node.get("description", ""),
        lean_declaration=decls,
        lean_file=node.get("lean_file") or node.get("mathlib_file", "(search the repo)"),
        id=node_id,
        criteria=crit,
        axioms="(not supplied — derive it yourself with `#print axioms` via `lake env lean`)",
    )


def parse_score(stdout: str, axis: str) -> dict:
    """Compatibility export for the shared structured-result parser."""
    return judge_runtime.parse_score(stdout, axis)


def run_judge(axis: str, prompt: str, repo: str, model: str, timeout: int) -> dict:
    """Run one rubric judge on the dispatcher-selected provider.

    The five-argument surface is intentionally stable: existing injected tests
    and downstream wrappers do not need provider-specific signatures.
    """
    return judge_runtime.run_judge(
        axis,
        prompt,
        repo,
        model,
        timeout,
        backend=_ACTIVE_JUDGE_BACKEND,
    )


def _worker_adapter(
    backend: str,
    repo: str,
    graph_path: str,
    max_wait_seconds: float,
):
    """The prover adapter for the configured backend (the engine's worker path).
    Every backend is explicit and imported lazily so the engine starts without
    optional provider dependencies."""
    if backend == "aristotle":
        from autoform.prover.aristotle_adapter import AristotleAdapter
        return AristotleAdapter(
            graph_path=graph_path,
            max_wait_seconds=max_wait_seconds,
        )
    if backend == "codex":
        from autoform.prover.codex_adapter import CodexAdapter
        return CodexAdapter(max_wait_seconds=max_wait_seconds)
    if backend in {"openai", "avocado"}:
        from autoform.prover.openai_adapter import OpenAICompatAdapter
        return OpenAICompatAdapter(
            graph_path=graph_path,
            preset=backend,
            max_wait_seconds=max_wait_seconds,
        )
    if backend == "claude":
        return _ClaudeAdapter(max_wait_seconds=max_wait_seconds)
    raise ValueError(
        f"unknown prover adapter {backend!r}; expected claude, aristotle, codex, openai, or avocado"
    )


def _verify_existing_target(node_id: str, node: dict, repo: str):
    """Kernel-check an already present target before spending a prover run."""
    lean_file = node.get("lean_file")
    declarations = node.get("lean_declarations") or []
    if not lean_file or _verify_existing_proof is None:
        return None
    target = target_state.target_lean_file(Path(repo), node_id, node)
    if not target.is_file():
        return None
    return _verify_existing_proof(
        node_id,
        repo,
        touched=[str(lean_file)],
        expected_files=[str(lean_file)],
        expected_declarations=list(declarations),
    )


def run_worker(
    node_id: str,
    node: dict,
    proj: Path,
    graph_path: str,
    repo: str,
    max_steers: int,
    backend: str = "claude",
    judge_backend: str = "claude",
    judge_model: str | None = None,
    judge_timeout: int = 180,
    worker_timeout: int = 600,
) -> tuple:
    """Prove/repair one node via the prover core (#14) on the chosen ``backend``.
    Serial — workers write files. Returns (status, reason, detail): status
    'proved'|'failed' (honest — gated by the driver's verification gate), reason =
    the one-line outcome, detail = the worker's fuller report — on a FAILED this
    carries its escalation prose (the named missing lemma, why it's stuck), which the
    engine hands to the orchestrator to triage rather than acting on itself."""
    if not _PROVER_OK:
        return "failed", f"prover core unavailable: {_PROVER_ERR}", ""
    spec = None
    if _build_node_spec:
        try:
            spec = _build_node_spec(Path(graph_path), node_id, project_dir=Path(repo))
        except Exception:
            spec = None
    if not spec:                              # fallback spec from the node's prose
        body = ""
        if node.get("content") and (proj / node["content"]).exists():
            body = (proj / node["content"]).read_text()[:4000]
        spec = (f"Target node `{node_id}` ({node.get('kind', 'statement')}). "
                f"{node.get('description', '')}\n\n{body}\n\n"
                f"Find the declaration(s) in the repo and complete/repair the proof so the file "
                f"compiles cleanly with NO sorry/admit/axiom — or report an honest FAILED.")
    try:                                       # the worker edits + builds autonomously
        steer_judge = _Steerer(
            judge=lambda prompt: judge_runtime.run_steer_judge(
                prompt,
                repo,
                judge_model,
                judge_timeout,
                backend=judge_backend,
            )
        )
        res = _prove(_worker_adapter(backend, repo, graph_path, worker_timeout),
                     node_id, spec, repo, max_steers=max_steers,
                     steerer=steer_judge,
                     expected_files=([node["lean_file"]] if node.get("lean_file") else None),
                     expected_declarations=(node.get("lean_declarations") or None))
        # The MCP prover records usage itself, but the deterministic dispatcher
        # calls the shared driver directly. Keep both entry points on the same
        # append-only ledger contract.
        from autoform.prover.runtime import _record_usage
        _record_usage(repo, node_id, backend, res)
        return res.status, (res.reason or ""), (res.proof_text or "")
    except Exception as e:
        return "failed", f"prover error: {e}", ""


_ESC_NOTE_CAP = 2400


def _escalation_note(reason: str, detail: str, cap: int = _ESC_NOTE_CAP) -> str:
    """Build the escalation ``note`` from the worker's one-line FAILED ``reason`` and
    its fuller ``detail`` (its final report). The FAILED line is the most actionable
    part, so it always leads and is never truncated; the report follows, kept
    head-AND-tail (with an explicit ``…[N chars omitted]…`` marker) when long — the
    named missing lemma is often in the report's *tail*, which a plain head-truncation
    would silently drop (the very signal the escalation exists to carry)."""
    reason = (reason or "").strip()
    detail = (detail or "").strip()
    extra = detail if detail and detail != reason else ""
    if not extra:
        return reason[:cap]
    budget = max(400, cap - len(reason) - 40)
    if len(extra) <= budget:
        clip = extra
    else:
        head, tail = budget * 3 // 5, budget * 2 // 5
        clip = f"{extra[:head]}\n…[{len(extra) - head - tail} chars omitted]…\n{extra[-tail:]}"
    return f"{reason}\n\n{clip}".strip() if reason else clip


def _raise_escalation(queue: list, node_id: str, label: str, note: str,
                      max_escalations: int = 3) -> bool:
    """Append an ``escalation`` task for the orchestrator to triage, with two
    engine-side circuit breakers so safety never rests on LLM prose alone:
      * **dedup** — at most one *open* (queued/running) escalation per node;
      * **cap** — at most ``max_escalations`` escalations per node *ever* (``done``
        ones count too). Past the cap the engine stops raising: a node still failing
        after N grow-the-DAG rounds is a human's call, not an infinite Max-billed
        retry loop. Returns True iff a new task was added.

    The engine NEVER mutates ``graph.json`` from a worker result — whether a wall is a
    real new prerequisite, a duplicate, a cluster-level gap, or a non-DAG failure
    (toolchain / false statement / honest give-up) is a judgment call. It only raises
    the flag + the worker's own words (``note``); ``/autoform:orchestrate`` decides."""
    escs = [x for x in queue if x.get("agent") == "escalation" and x.get("node") == node_id]
    if any(e.get("status") in ("queued", "running") for e in escs):
        return False                      # an open escalation is already pending
    if len(escs) >= max_escalations:
        return False                      # cap hit — stop the retry/escalate cycle
    queue.append({
        "id": dq.new_task_id("escalation", node_id, queue),
        "agent": "escalation", "node": node_id, "node_label": (label or node_id),
        "status": "queued", "at": dq._now(), "source": "engine", "note": note})
    return True


def _node_escalations(queue: list, node_id: str) -> list:
    """All escalation tasks for a node (any status) — for the worker guard + cap."""
    return [x for x in queue if x.get("agent") == "escalation" and x.get("node") == node_id]


def sweep_stale_running(queue_path: Path, feed_path: Path) -> int:
    """Crash recovery, run once at engine startup: reset every reviewer/worker task
    stranded in ``running`` back to ``queued`` (with a note appended), under the
    cross-process lock. Returns the number of tasks recovered.

    A task is only ever ``running`` while THIS engine is executing it, so at startup
    any ``running`` engine-kind task is a leftover of an engine that died mid-flight.
    Without the sweep such a task is unrecoverable: the drain takes only ``queued``,
    the dashboard can't cancel a ``running`` task, and both dedups (server + CLI)
    block a re-enqueue. Only the engine's own kinds are swept — an orchestrator-owned
    task (escalation/planner/…) may legitimately be ``running`` in another session."""
    with fslock.locked(queue_path):
        queue = dq.load_queue(queue_path)
        n = 0
        for t in queue:
            if (isinstance(t, dict) and t.get("agent") in dq._ENGINE_KINDS
                    and t.get("status") == "running"):
                t["status"] = "queued"
                t.pop("started_at", None)
                note = str(t.get("note") or "").strip()
                stamp = "requeued after engine restart"
                t["note"] = f"{note} · {stamp}" if note else stamp
                n += 1
        if n:
            dq._save(queue_path, queue)
            dq.sync_feed(feed_path, queue)
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic parallel review dispatcher.")
    ap.add_argument("project", type=Path, help="dir holding graph.json + task_queue.json")
    ap.add_argument("--repo", type=Path, default=None, help="Lean repo = judge cwd (default: graph metadata.lean_root, else <project>/../..)")
    ap.add_argument("--jobs", type=int, default=max(3, 3 * len(AXES)), help=f"max concurrent judges (default = 3 nodes x {len(AXES)} axes)")
    ap.add_argument("--judge-backend", choices=judge_runtime.SUPPORTED_JUDGES,
                    default=os.environ.get("AUTOFORM_JUDGE_BACKEND", "claude"),
                    help="jury provider (default: AUTOFORM_JUDGE_BACKEND or claude)")
    ap.add_argument("--model", default=None, help="judge model override (provider default when omitted)")
    ap.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="wall-clock seconds allowed for each judge and worker run (default 600)",
    )
    ap.add_argument("--limit", type=int, default=0, help="process only the first N reviewer tasks (0 = all)")
    ap.add_argument("--watch", action="store_true", help="keep running: drain, then re-poll for new drops every --poll s (Ctrl-C to stop)")
    ap.add_argument("--poll", type=int, default=10, help="seconds between polls in --watch (default 10)")
    ap.add_argument("--workers", action="store_true", help="ALSO drain worker tasks (serial) via the prover core — proves/repairs nodes")
    ap.add_argument("--max-steers", type=int, default=2, help="worker: max live steers per node (default 2)")
    ap.add_argument(
        "--backend",
        choices=tuple(backend_config.BACKENDS),
        default=None,
        help="prover backend for --workers (default: persisted backend_config)",
    )
    ap.add_argument(
        "--allow-api-egress",
        action="append",
        choices=("openai", "avocado"),
        default=[],
        metavar="PROVIDER",
        help=(
            "confirm project-data egress to one API provider for this process; "
            "repeat when prover and judge use different providers"
        ),
    )
    ap.add_argument("--max-escalations", type=int, default=3, help="worker: engine-side bound — stop re-proving/re-escalating a node after this many escalations (default 3), so a hard node can't loop forever")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.timeout <= 0:
        ap.error("--timeout must be greater than zero")
    if a.jobs <= 0:
        ap.error("--jobs must be greater than zero")
    if a.poll <= 0:
        ap.error("--poll must be greater than zero")
    if a.max_steers < 0:
        ap.error("--max-steers cannot be negative")
    if a.max_escalations < 0:
        ap.error("--max-escalations cannot be negative")
    if a.backend is None:
        try:
            a.backend = backend_config.get_backend()
        except ValueError as error:
            ap.error(str(error))
    required_egress = {
        provider
        for provider in (a.judge_backend,)
        if provider in {"openai", "avocado"}
    }
    if a.workers and a.backend in {"openai", "avocado"}:
        required_egress.add(a.backend)
    missing_egress = required_egress - set(a.allow_api_egress)
    if missing_egress:
        ap.error(
            "explicit per-run API egress confirmation required: add "
            + " ".join(
                f"--allow-api-egress {provider}"
                for provider in sorted(missing_egress)
            )
            + " after reviewing provider/base URL and project data scope"
        )
    if a.dry_run:
        return _run_dispatch(a)
    try:
        with dispatcher_lease(a.project):
            return _run_dispatch(a)
    except dq.QueueStateError as error:
        print(f"queue state error: {error}", file=sys.stderr)
        return 2
    except (DispatcherBusy, ValueError, OSError) as error:
        print(f"dispatcher startup failed: {error}", file=sys.stderr)
        return 3


def _run_dispatch(a) -> int:
    global _ACTIVE_JUDGE_BACKEND
    _ACTIVE_JUDGE_BACKEND = a.judge_backend

    proj = a.project
    graph_path = proj / "graph.json"
    graph = json.loads(graph_path.read_text())
    sidecar_path = proj / "review_status.json"
    queue_path = proj / "task_queue.json"
    feed_path = proj / "agents_status.json"
    configured_root = graph.get("metadata", {}).get("lean_root")
    if configured_root and not Path(str(configured_root)).is_absolute():
        configured_root = str((proj / str(configured_root)).resolve())
    repo = str(a.repo or configured_root or _nearest_lean_root(proj))

    try:
        dq.load_queue(queue_path)
    except dq.QueueStateError as error:
        print(f"queue state error: {error}", file=sys.stderr)
        return 2

    if not a.dry_run:                       # crash recovery: un-strand 'running' tasks
        swept = sweep_stale_running(queue_path, feed_path)
        if swept:
            print(f"recovered {swept} task(s) stranded in 'running' → re-queued "
                  f"(previous engine died mid-flight)", flush=True)

    initial = [t for t in dq.load_queue(queue_path)
               if t.get("status") == "queued" and t.get("agent") == "reviewer"]
    print(f"project          : {proj}")
    print(f"repo (judge cwd) : {repo}")
    print(f"queued reviewers : {len(initial)}")
    judge_model = a.model or "provider default"
    print(f"parallelism      : up to {a.jobs} concurrent judges · model {judge_model} · judges→{a.judge_backend}"
          + (f" · workers→{a.backend} ({backend_config.prover_of(a.backend)} adapter)" if a.workers else "")
          + (f" · WATCH every {a.poll}s" if a.watch else ""))
    if a.dry_run:
        for t in (initial[:a.limit] if a.limit else initial):
            print(f"  reviewer → {t['node']:28} → {len(AXES)}-judge jury ({' | '.join(AXES)})")
        return 0

    _rubric_warned = [False]                # one diagnostic per bad state, not per poll

    def current_nodes() -> dict:
        """Read the latest graph snapshot for each drain/task in watch mode."""
        return json.loads(graph_path.read_text()).get("nodes", {})

    def usable_rubrics():
        """Reload + validate the rubrics BEFORE any task is claimed.

        Every jury axis must have a rubric file carrying a ``prompt_template``
        (internal/rubrics/*.json). Returns the rubric
        dict when complete; otherwise prints one clear diagnostic and returns None,
        so the caller leaves every task queued instead of claiming work it would
        then crash on (KeyError at rubrics[axis])."""
        rubrics = load_rubrics()
        missing = [ax for ax in AXES
                   if not isinstance((rubrics.get(ax) or {}).get("prompt_template"), str)]
        if not missing:
            _rubric_warned[0] = False
            return rubrics
        if not _rubric_warned[0]:
            _rubric_warned[0] = True
            print(f"Autoform rubric data not found — no rubric with a prompt_template for "
                  f"axis(es): {', '.join(missing)} (looked in internal/rubrics/). "
                  f"Reinstall Autoform; reviewer tasks stay QUEUED until then.",
                  flush=True)
        return None

    def fail_task(tid: str, err: str) -> None:
        """Mark ONE task failed with the error in ``result`` — an unexpected
        exception sinks that task, never the loop/engine."""
        with fslock.locked(queue_path):
            cur = dq.load_queue(queue_path)
            for t in cur:
                if t["id"] == tid:
                    t["status"], t["finished_at"] = "failed", dq._now()
                    t["result"] = f"error: {err}"[:300]
            dq._save(queue_path, cur)
            dq.sync_feed(feed_path, cur)
        print(f"  ✗ task {tid} → FAILED ({err})", flush=True)

    def drain_once() -> int:
        """Review every currently-queued reviewer node in parallel; returns the count."""
        rubrics = usable_rubrics()          # validate BEFORE claiming anything
        if rubrics is None:
            return 0                        # tasks stay queued — nothing was claimed
        nodes = current_nodes()             # graph may grow between watch polls
        # Load-mutate-save cycles on the queue run under the cross-process lock —
        # the dashboard enqueues/cancels in the same file concurrently.
        with fslock.locked(queue_path):
            queue = dq.load_queue(queue_path)
            rev = [t for t in queue if t.get("status") == "queued" and t.get("agent") == "reviewer"]
            if a.limit:
                rev = rev[:a.limit]
            if not rev:
                return 0
            rev_ids = {t["id"] for t in rev}
            for t in queue:                      # claim up front → the feed shows them all running
                if t["id"] in rev_ids:
                    t["status"], t["started_at"] = "running", dq._now()
            dq._save(queue_path, queue)
            dq.sync_feed(feed_path, queue)

        results: dict[str, dict] = {t["id"]: {} for t in rev}
        review_fingerprints: dict[str, str] = {}
        lock = threading.Lock()

        def finalize(tid: str, node_id: str) -> None:
            scores = {ax: results[tid].get(ax, {}).get("score") for ax in AXES}
            usable = {k: v for k, v in scores.items() if isinstance(v, int)}
            if not usable:                  # every judge failed/timed out/abstained
                reasons = "; ".join(
                    str(results[tid].get(ax, {}).get("reasoning", ""))[:80] for ax in AXES)
                fail_task(tid, f"no usable scores — {reasons}")   # no ai verdict at all
                return
            # jury_verdict on whatever scores ARE usable: it is already conservative
            # about gaps (a missing axis blocks 'clean', a present failing correctness
            # axis rejects) — a judge timeout must never DOWNGRADE rejected→flagged.
            verdict = rm.jury_verdict(usable)
            fingerprint = review_fingerprints.get(tid)
            if fingerprint is not None:
                current = current_nodes().get(node_id)
                if not isinstance(current, dict):
                    fail_task(tid, "target disappeared while review was running")
                    return
                try:
                    current_fingerprint = target_state.artifact_fingerprint(
                        proj, Path(repo), node_id, current
                    )
                except target_state.TargetStateError as error:
                    fail_task(tid, str(error))
                    return
                if (
                    current_fingerprint != fingerprint
                    or current.get("proof_fingerprint") != fingerprint
                ):
                    fail_task(tid, "target changed while review was running")
                    return
            with fslock.locked(sidecar_path):       # vs the dashboard's human verdicts
                sc = rm.load_sidecar(sidecar_path)
                sc["reviews"].setdefault(node_id, {})["ai"] = {
                    **scores, "verdict": verdict, "at": _now(), "source": "dispatch:runner",
                    **({"fingerprint": fingerprint} if fingerprint is not None else {}),
                }
                rm.save_sidecar(sidecar_path, sc)   # preserves any human slot
            with fslock.locked(queue_path):         # re-read: new drops may have arrived
                cur = dq.load_queue(queue_path)
                for t in cur:
                    if t["id"] == tid:
                        t["status"], t["finished_at"] = "done", dq._now()
                        t["result"] = f"{verdict} (" + " ".join(f"{ax[0]}{scores[ax]}" for ax in AXES) + ")"
                dq._save(queue_path, cur)
                dq.sync_feed(feed_path, cur)
            print(f"  ✓ {node_id:28} → {verdict.upper():9} {scores}", flush=True)

        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            fut_map = {}
            for t in rev:
                try:                        # per-task: a bad node/prompt fails THAT task
                    node = nodes.get(t["node"], {})
                    if node.get("proof_status") == "proved":
                        fingerprint = target_state.artifact_fingerprint(
                            proj, Path(repo), t["node"], node
                        )
                        if node.get("proof_fingerprint") != fingerprint:
                            raise ValueError("proved target fingerprint is missing or stale")
                        review_fingerprints[t["id"]] = fingerprint
                    content_text = ""
                    if node.get("content") and (proj / node["content"]).exists():
                        content_text = (proj / node["content"]).read_text()
                    prompts = [(axis, build_prompt(rubrics[axis], t["node"], node, content_text))
                               for axis in AXES]
                except Exception as e:
                    fail_task(t["id"], str(e))
                    continue
                for axis, prompt in prompts:
                    fut = ex.submit(run_judge, axis, prompt, repo, a.model, a.timeout)
                    fut_map[fut] = (t["id"], t["node"], axis)
            for fut in cf.as_completed(fut_map):
                tid, node_id, axis = fut_map[fut]
                try:
                    res = fut.result()
                except Exception as e:                          # never let one judge sink the run
                    res = {"score": None, "reasoning": f"{axis}: {e}", "error": "exc"}
                print(f"    [{node_id}] {axis:16} score={res.get('score')}", flush=True)
                with lock:
                    results[tid][axis] = res
                    if len(results[tid]) == len(AXES):
                        try:                # per-task: a finalize blow-up fails THAT task
                            finalize(tid, node_id)
                        except Exception as e:
                            fail_task(tid, str(e))
        return len(rev)

    def drain_workers() -> int:
        """Prove every queued worker node, one at a time (workers write files → serial)."""
        if not a.workers:
            return 0
        queue = dq.load_queue(queue_path)
        wk = [t for t in queue if t.get("status") == "queued" and t.get("agent") == "worker"]
        n = 0
        for t in wk:
            try:
                n += _drain_one_worker(t)
            except Exception as e:          # per-task: an unexpected blow-up fails THAT task
                fail_task(t["id"], str(e))
                n += 1
        return n

    def _drain_one_worker(t: dict) -> int:
        """Claim + prove one queued worker task; returns 1 when it was handled
        (proved / failed / blocked), 0 when skipped (open escalation)."""
        nodes = current_nodes()             # each serial worker gets the latest graph
        node = nodes.get(t["node"], {})
        expected_spec_fingerprint = None
        if "proof_status" in node:
            declarations = node.get("lean_declarations")
            roadmap_id = node.get("roadmap_id")
            try:
                target_state.target_lean_file(Path(repo), t["node"], node)
            except target_state.TargetStateError as error:
                fail_task(t["id"], str(error))
                return 1
            if not isinstance(declarations, list) or not declarations or not all(
                isinstance(name, str) and name.strip() for name in declarations
            ):
                fail_task(t["id"], "explicit target has no non-empty lean_declarations")
                return 1
            if not isinstance(roadmap_id, str) or not roadmap_id:
                fail_task(t["id"], "explicit target has no non-empty roadmap_id")
                return 1
        if "proof_status" in node and (
            node.get("spec_status") != "ready" or node.get("proof_status") != "pending"
        ):
            fail_task(
                t["id"],
                "target is not proof-ready: expected spec_status='ready' and "
                f"proof_status='pending', got spec_status={node.get('spec_status')!r} "
                f"proof_status={node.get('proof_status')!r}",
            )
            return 1
        if "proof_status" in node:
            expected_spec_fingerprint = target_state.spec_fingerprint(proj, t["node"], node)
        cap_hit = False
        capped_escalations = 0
        with fslock.locked(queue_path):     # re-read (new drops/escalations) + claim
            c = dq.load_queue(queue_path)
            current = next((x for x in c if x.get("id") == t.get("id")), None)
            if current is None or current.get("status") != "queued":
                return 0
            escs = _node_escalations(c, t["node"])
            # Engine-side enforcement of the doc's guard — don't rely on LLM prose:
            if any(e.get("status") in ("queued", "running") for e in escs):
                return 0                  # an open escalation on this node — leave it queued, skip
            if len(escs) >= a.max_escalations:        # cap hit — stop re-proving a hard node
                cap_hit = True
                capped_escalations = len(escs)
                for x in c:
                    if x["id"] == t["id"]:
                        x["status"], x["finished_at"] = "failed", dq._now()
                        x["result"] = f"blocked: {len(escs)} escalations exhausted — needs human"
                dq._save(queue_path, c)
                dq.sync_feed(feed_path, c)
            else:
                for x in c:                                             # claim
                    if x["id"] == t["id"]:
                        x["status"], x["started_at"] = "running", dq._now()
                dq._save(queue_path, c)
                dq.sync_feed(feed_path, c)
        if cap_hit:
            assert expected_spec_fingerprint is not None or "proof_status" not in node
            if expected_spec_fingerprint is not None:
                _mark_proof_blocked(
                    graph_path, proj, t["node"], expected_spec_fingerprint
                )
            print(
                f"  ⛔ worker {t['node']:24} → BLOCKED "
                f"({capped_escalations} escalations, capped)",
                flush=True,
            )
            return 1
        existing_gate = (
            _verify_existing_target(t["node"], node, repo)
            if not node.get("proof_fingerprint")
            else None
        )
        if existing_gate is not None and existing_gate.ok:
            print(f"  ✓ existing target → {t['node']} (kernel gate clean)", flush=True)
            status, reason, detail = "proved", "existing target verified", ""
        else:
            print(f"  ⛏ worker → {t['node']} (proving…)", flush=True)
            status, reason, detail = run_worker(t["node"], node, proj,
                                                str(proj / "graph.json"), repo, a.max_steers,
                                                backend=backend_config.prover_of(a.backend),
                                                judge_backend=a.judge_backend,
                                                judge_model=a.model,
                                                judge_timeout=min(a.timeout, 180),
                                                worker_timeout=a.timeout)
        explicit_target = "proof_status" in nodes.get(t["node"], {})
        if status == "proved" and explicit_target:
            try:
                _invalidate_ai_review(sidecar_path, t["node"])
                assert expected_spec_fingerprint is not None
                _mark_proof_verified(
                    graph_path,
                    proj,
                    Path(repo),
                    t["node"],
                    expected_spec_fingerprint,
                )
            except Exception as error:
                status = "failed"
                reason = f"proof landed but durable completion update failed: {error}"
        with fslock.locked(queue_path):     # finish (re-read for new drops)
            c = dq.load_queue(queue_path)
            for x in c:
                if x["id"] == t["id"]:
                    x["status"] = "done" if status == "proved" else "failed"
                    x["finished_at"], x["result"] = dq._now(), f"{status}: {reason[:160]}"
            escalated = False
            if status == "proved" and explicit_target:
                if not any(
                    x.get("agent") == "reviewer"
                    and x.get("node") == t["node"]
                    and x.get("status") in ("queued", "running")
                    for x in c
                ):
                    c.append({
                        "id": dq.new_task_id("reviewer", t["node"], c),
                        "agent": "reviewer",
                        "node": t["node"],
                        "node_label": (nodes.get(t["node"], {}).get("description")
                                       or t["node"])[:60],
                        "status": "queued",
                        "at": dq._now(),
                        "source": "engine:verified-proof",
                    })
            elif status != "proved":  # hand the worker's wall to the orchestrator — it decides, not us
                lbl = (nodes.get(t["node"], {}).get("description") or t["node"])[:60]
                escalated = _raise_escalation(c, t["node"], lbl,
                                              _escalation_note(reason, detail), a.max_escalations)
            dq._save(queue_path, c)
            dq.sync_feed(feed_path, c)
        print(f"  {'✓' if status == 'proved' else '✗'} worker {t['node']:24} → {status.upper()}"
              + ("  ⚑ escalation raised" if escalated else ""), flush=True)
        return 1

    if a.watch:
        print("WATCHING — drop reviewers on the dashboard and they auto-fire. Ctrl-C to stop.", flush=True)
        total = 0
        try:
            while True:
                try:
                    n = drain_once() + drain_workers()
                except Exception:           # the watch loop survives anything but Ctrl-C
                    traceback.print_exc()
                    print("  engine error — surviving it; will keep draining.", flush=True)
                    n = 0
                if n:
                    total += n
                    print(f"  …drained {n} (session total {total}); re-checking for new drops.", flush=True)
                else:
                    dq.sync_feed(feed_path, dq.load_queue(queue_path))
                    time.sleep(a.poll)
        except KeyboardInterrupt:
            dq.sync_feed(feed_path, dq.load_queue(queue_path))
            print(f"\nstopped — {total} reviewer node(s) scored this session.", flush=True)
        return 0

    n = drain_once() + drain_workers()
    dq.sync_feed(feed_path, dq.load_queue(queue_path))
    print(f"\nDONE — {n} task(s) processed. Sidecar: {sidecar_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
