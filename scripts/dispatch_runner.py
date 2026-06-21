#!/usr/bin/env python3
"""Deterministic parallel dispatcher for the DAG review dashboard.

Reads ``task_queue.json`` and fans work out as parallel ``claude -p`` processes —
with **no reliance on an LLM orchestrator choosing to delegate**. Each REVIEWER
task spawns the 3-judge jury (faithfulness / proof_integrity / code_quality)
concurrently; ALL queued nodes' judges run in one bounded process pool, so nodes
are reviewed **in parallel, not one-by-one**. The single parent process is the
only writer of ``review_status.json`` (atomic, under a lock) — no write race.

Billing: every judge runs ``claude -p`` with ``ANTHROPIC_API_KEY`` scrubbed → the
Max subscription. Judges get only Read/Grep/Glob/Bash (read the Lean, run
``#print axioms``) — never write the verdict file themselves; the parent does.

Usage::

  env -u ANTHROPIC_API_KEY python3 scripts/dispatch_runner.py <project-dir> \\
      [--repo <lean-repo>] [--jobs 9] [--model opus] [--limit N] [--dry-run]

``<project-dir>`` holds graph.json + task_queue.json + review_status.json.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "review_ui"))
sys.path.insert(0, str(HERE))
import review_model as rm          # load_sidecar / save_sidecar / jury_verdict
import dispatch_queue as dq        # _save / _feed_for / _now (queue + live feed)
sys.path.insert(0, str(HERE.parent))   # plugin root, for the prover core (--workers)
try:
    from servers.prover.driver import prove as _prove
    from servers.prover.claude_adapter import ClaudeAdapter as _ClaudeAdapter
    try:
        from servers.aristotle.core import build_node_spec as _build_node_spec
    except Exception:
        _build_node_spec = None
    _PROVER_OK, _PROVER_ERR = True, ""
except Exception as _e:                 # prover deps absent → --workers reports it cleanly
    _PROVER_OK, _PROVER_ERR, _build_node_spec = False, str(_e), None

RUBRIC_DIR = HERE.parent / "skills" / "eval-rubrics" / "references"
AXES = ["faithfulness", "proof_integrity", "code_quality"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_rubrics() -> dict:
    return {ax: json.loads((RUBRIC_DIR / f"{ax}.json").read_text()) for ax in AXES}


def build_prompt(rubric: dict, node_id: str, node: dict, content_text: str) -> str:
    """Fill the rubric's prompt_template from the node's graph data + prose."""
    crit = "\n".join(f"{k}: {v}" for k, v in rubric["criteria"].items())
    decls = ", ".join(node.get("mathlib_declarations") or []) \
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
        lean_file=node.get("mathlib_file", "(search the repo)"),
        id=node_id,
        criteria=crit,
        axioms="(not supplied — derive it yourself with `#print axioms` via `lake env lean`)",
    )


def _balanced_objects(text: str) -> list:
    """Every top-level {...} object in `text`, brace-balanced (handles NESTED JSON
    like proof_integrity's `axiom_verdicts`, which a non-greedy regex cannot)."""
    objs, depth, start = [], 0, None
    in_str, esc = False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                objs.append(text[start:i + 1])
                start = None
    return objs


def parse_score(stdout: str, axis: str) -> dict:
    """Pull {score, reasoning} from a `claude -p --output-format json` result."""
    text = (stdout or "").strip()
    try:                                  # unwrap the claude envelope {type,result,...}
        env = json.loads(text)
        if isinstance(env, dict) and "result" in env:
            text = env["result"]
    except Exception:
        pass
    for cand in reversed(_balanced_objects(text)):   # last balanced obj carrying a score
        try:
            j = json.loads(cand)
        except Exception:
            continue
        if isinstance(j, dict) and "score" in j:
            s = j.get("score")
            return {"score": int(s) if isinstance(s, (int, float)) else None,
                    "reasoning": str(j.get("reasoning", ""))[:500]}
    return {"score": None, "reasoning": f"{axis}: unparseable output: {text[:160]}", "error": "parse"}


def run_judge(axis: str, prompt: str, repo: str, model: str, timeout: int) -> dict:
    sysp = (f"You are the autoform {axis} judge. Score ONLY this one axis, strictly per the rubric in "
            f"the prompt. Investigate the real Lean in your working directory (read the declarations; "
            f"for proof_integrity run `#print axioms`). Do NOT write review_status.json — only output "
            f"your JSON verdict as the final message.")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}   # -> Max, never the API
    args = ["claude", "-p", prompt, "--append-system-prompt", sysp,
            "--allowedTools", "Read,Grep,Glob,Bash", "--output-format", "json", "--model", model]
    try:
        p = subprocess.run(args, env=env, cwd=repo, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"score": None, "reasoning": f"{axis}: judge timed out after {timeout}s", "error": "timeout"}
    if p.returncode != 0 and not p.stdout.strip():
        return {"score": None, "reasoning": f"{axis}: claude exited {p.returncode}: {p.stderr[:160]}", "error": "exit"}
    return parse_score(p.stdout, axis)


def run_worker(node_id: str, node: dict, proj: Path, graph_path: str, repo: str, max_steers: int) -> tuple:
    """Prove/repair one node via the prover core (#14). Serial — workers write files.
    Returns (status, reason); status is 'proved' or 'failed' (honest, never faked)."""
    if not _PROVER_OK:
        return "failed", f"prover core unavailable: {_PROVER_ERR}"
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
    try:                                       # skip-permissions: the worker edits + builds autonomously
        res = _prove(_ClaudeAdapter(extra_args=["--dangerously-skip-permissions"]),
                     node_id, spec, repo, max_steers=max_steers)
        return res.status, (res.reason or "")
    except Exception as e:
        return "failed", f"prover error: {e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic parallel review dispatcher.")
    ap.add_argument("project", type=Path, help="dir holding graph.json + task_queue.json")
    ap.add_argument("--repo", type=Path, default=None, help="Lean repo = judge cwd (default: graph metadata.lean_root, else <project>/../..)")
    ap.add_argument("--jobs", type=int, default=9, help="max concurrent claude judges (default 9 = 3 nodes x 3)")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--limit", type=int, default=0, help="process only the first N reviewer tasks (0 = all)")
    ap.add_argument("--watch", action="store_true", help="keep running: drain, then re-poll for new drops every --poll s (Ctrl-C to stop)")
    ap.add_argument("--poll", type=int, default=10, help="seconds between polls in --watch (default 10)")
    ap.add_argument("--workers", action="store_true", help="ALSO drain worker tasks (serial) via the prover core — proves/repairs nodes")
    ap.add_argument("--max-steers", type=int, default=2, help="worker: max live steers per node (default 2)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    proj = a.project
    graph = json.loads((proj / "graph.json").read_text())
    nodes = graph.get("nodes", {})
    sidecar_path = proj / "review_status.json"
    queue_path = proj / "task_queue.json"
    feed_path = proj / "agents_status.json"
    repo = str(a.repo or graph.get("metadata", {}).get("lean_root") or proj.parent.parent)
    rubrics = load_rubrics()

    initial = [t for t in (json.loads(queue_path.read_text()) if queue_path.exists() else [])
               if t.get("status") == "queued" and t.get("agent") == "reviewer"]
    print(f"project          : {proj}")
    print(f"repo (judge cwd) : {repo}")
    print(f"queued reviewers : {len(initial)}")
    print(f"parallelism      : up to {a.jobs} concurrent judges · model {a.model} · backend max (key scrubbed)"
          + (f" · WATCH every {a.poll}s" if a.watch else ""))
    if a.dry_run:
        for t in (initial[:a.limit] if a.limit else initial):
            print(f"  reviewer → {t['node']:28} → 3-judge jury (faithfulness | proof_integrity | code_quality)")
        return 0

    def drain_once() -> int:
        """Review every currently-queued reviewer node in parallel; returns the count."""
        queue = json.loads(queue_path.read_text()) if queue_path.exists() else []
        rev = [t for t in queue if t.get("status") == "queued" and t.get("agent") == "reviewer"]
        if a.limit:
            rev = rev[:a.limit]
        if not rev:
            return 0
        rev_ids = {t["id"] for t in rev}
        for t in queue:                          # claim up front → the feed shows them all running
            if t["id"] in rev_ids:
                t["status"], t["started_at"] = "running", dq._now()
        dq._save(queue_path, queue)
        dq._save(feed_path, dq._feed_for(queue))

        results: dict[str, dict] = {t["id"]: {} for t in rev}
        lock = threading.Lock()

        def finalize(tid: str, node_id: str) -> None:
            scores = {ax: results[tid].get(ax, {}).get("score") for ax in AXES}
            usable = {k: v for k, v in scores.items() if isinstance(v, int)}
            verdict = rm.jury_verdict(usable) if len(usable) == 3 else "flagged"
            sc = rm.load_sidecar(sidecar_path)                  # single writer, under lock
            sc["reviews"].setdefault(node_id, {})["ai"] = {
                **scores, "verdict": verdict, "at": _now(), "source": "dispatch:runner"}
            rm.save_sidecar(sidecar_path, sc)                   # preserves any human slot
            cur = json.loads(queue_path.read_text())            # re-read: new drops may have arrived
            for t in cur:
                if t["id"] == tid:
                    t["status"], t["finished_at"] = "done", dq._now()
                    t["result"] = f"{verdict} (f{scores['faithfulness']}/i{scores['proof_integrity']}/q{scores['code_quality']})"
            dq._save(queue_path, cur)
            dq._save(feed_path, dq._feed_for(cur))
            print(f"  ✓ {node_id:28} → {verdict.upper():9} {scores}", flush=True)

        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            fut_map = {}
            for t in rev:
                node = nodes.get(t["node"], {})
                content_text = ""
                if node.get("content") and (proj / node["content"]).exists():
                    content_text = (proj / node["content"]).read_text()
                for axis in AXES:
                    prompt = build_prompt(rubrics[axis], t["node"], node, content_text)
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
                        finalize(tid, node_id)
        return len(rev)

    def drain_workers() -> int:
        """Prove every queued worker node, one at a time (workers write files → serial)."""
        if not a.workers:
            return 0
        queue = json.loads(queue_path.read_text()) if queue_path.exists() else []
        wk = [t for t in queue if t.get("status") == "queued" and t.get("agent") == "worker"]
        n = 0
        for t in wk:
            c = json.loads(queue_path.read_text())                      # claim
            for x in c:
                if x["id"] == t["id"]:
                    x["status"], x["started_at"] = "running", dq._now()
            dq._save(queue_path, c); dq._save(feed_path, dq._feed_for(c))
            print(f"  ⛏ worker → {t['node']} (proving…)", flush=True)
            status, reason = run_worker(t["node"], nodes.get(t["node"], {}), proj,
                                        str(proj / "graph.json"), repo, a.max_steers)
            c = json.loads(queue_path.read_text())                      # finish (re-read for new drops)
            for x in c:
                if x["id"] == t["id"]:
                    x["status"] = "done" if status == "proved" else "failed"
                    x["finished_at"], x["result"] = dq._now(), f"{status}: {reason[:160]}"
            dq._save(queue_path, c); dq._save(feed_path, dq._feed_for(c))
            print(f"  {'✓' if status == 'proved' else '✗'} worker {t['node']:24} → {status.upper()}", flush=True)
            n += 1
        return n

    idle = {"orchestrator": {"state": "idle"}, "agents": []}
    if a.watch:
        print("WATCHING — drop reviewers on the dashboard and they auto-fire. Ctrl-C to stop.", flush=True)
        total = 0
        try:
            while True:
                n = drain_once() + drain_workers()
                if n:
                    total += n
                    print(f"  …drained {n} (session total {total}); re-checking for new drops.", flush=True)
                else:
                    dq._save(feed_path, idle)
                    time.sleep(a.poll)
        except KeyboardInterrupt:
            dq._save(feed_path, idle)
            print(f"\nstopped — {total} reviewer node(s) scored this session.", flush=True)
        return 0

    n = drain_once() + drain_workers()
    dq._save(feed_path, idle)
    print(f"\nDONE — {n} task(s) processed. Sidecar: {sidecar_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
