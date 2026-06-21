---
description: The autoform orchestrator — launch the deterministic dispatch engine (parallel review jury + prover workers) and, by default, autonomously drive the formalization to a clean trust frontier; or let the human drive it via the dashboard, or both, off one shared queue.
argument-hint: "[<review-project-dir>] [--manual] [--max-tasks N] [--backend max|aristotle|codex] [--once]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, Skill
---

# /autoform:orchestrate — the dispatch engine + autonomous driver

Two things run, sharing **one task queue** (`task_queue.json`, the file the dashboard writes when a human drops an agent on a node):

- a **deterministic background engine** — `scripts/dispatch_runner.py --watch --workers` — that drains the queue continuously: **reviewers** as a parallel 3-judge jury, **workers** via the prover core, billed to Max (key scrubbed). It handles human drops AND the tasks you queue, idempotently.
- **you**, the orchestrator — who (by default) inspect the graph and **self-queue** the next work foundations-first, and run **planner** tasks via the splitter subagent.

So it runs **autonomously**, the **human drives** it via the dashboard, or **both**. Arguments: `$ARGUMENTS`.

## ⛔ You never review or prove a node yourself
Reviewing and proving are the engine's job (deterministic, parallel). You only: launch the engine, decide what to queue, `enqueue` it, run planners, and report. If you catch yourself scoring a node or editing a `.lean` proof, STOP — that's the engine.

## Setup
1. **Project dir** (holds `graph.json` + `task_queue.json`): explicit `$ARGUMENTS` > `$AUTOFORM_DISPATCH_PROJECT` > the running dashboard (`ps -axww -o command | grep '[s]erve_review.py'` → its `--graph` parent) > ask. **Echo it.** Note `metadata.lean_root` → **PROJECT_DIR** (where proofs land).
2. **Backend**: `--backend` > `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/backend_config.py get` > `max`. **Echo it.**

## 1. Launch the engine (always)
Detached + idempotent — skip if a `--watch` engine is already up:
```
pgrep -f "dispatch_runner.py.*--watch" >/dev/null \
  && echo "engine already running (pkill -f dispatch_runner.py to stop)" \
  || { nohup env -u ANTHROPIC_API_KEY python3 -u ${CLAUDE_PLUGIN_ROOT}/scripts/dispatch_runner.py <project> --repo <PROJECT_DIR> --jobs 9 --watch --workers >> <project>/dispatch.log 2>&1 & echo "started engine PID $!"; }
```
`--workers` proves nodes autonomously (the prover worker runs with skip-permissions). **NEVER run a `--watch` in the foreground** — it loops forever and hangs this command. Tell the user: drops auto-fire; `tail -f <project>/dispatch.log` to watch; `pkill -f dispatch_runner.py` to stop.

## 2. Drive it — default FULL-AUTO (`--manual` = drop-only)
Unless `--manual`, run the autonomy loop until every target's closure is clean or `--max-tasks` (default 40) is hit:

1. **Read** `graph.json` + `review_status.json`. Compute, **foundations-first** (topological by `depends_on`):
   - **unreviewed** node (no `ai` or `human` verdict) → queue a **reviewer**.
   - **defective / unproven** node (`rejected`/`flagged` verdict, a `sorry`, or `mathlib_status:"missing"` with no proof) whose prerequisites are already clean → queue a **worker**.
   - a node too coarse to act on (a tier-1 cluster with no fine children) → a **planner** (you run it, step 3).
2. **Enqueue a bounded wave** (≈ `--jobs`×2 reviewers + a few workers — not the whole graph at once), each via:
   ```
   env -u ANTHROPIC_API_KEY python3 ${CLAUDE_PLUGIN_ROOT}/scripts/dispatch_queue.py <project> enqueue --agent <reviewer|worker> --node <id>
   ```
   `enqueue` dedups, but still skip nodes already `running`. Don't queue a worker whose prerequisites are still `rejected`.
3. **Planners are yours** (the engine does reviewers + workers only): `Task subagent_type:"autoform:splitter"` over the node's scope → merge results through `scripts/merge_node.py`.
4. **Wait for the wave**: poll `dispatch_queue.py <project> status` (and `dispatch.log`) until the queued tasks reach `done`/`failed` — the engine updates them. Then **re-read the graph and queue the next wave**: each clean proof unblocks dependents, each clean review exposes the next frontier.
5. **Stop** at a clean frontier or `--max-tasks`. Summarize: nodes reviewed/proved, verdict counts (clean/flagged/rejected), and the remaining frontier.

The human can drop tasks in the dashboard at any time — they land in the same queue and the engine drains them exactly like yours. Autonomous and manual coexist.

## Honesty (non-negotiable)
- The engine records the jury's **actual** verdict and marks a worker `done` ONLY on a real `proved` (sorry gone, build clean, no `sorryAx`) — never a faked proof. Don't override it.
- The live feed mirrors real state; never fabricate `running`/`done`.

## --once
A single foreground drain of the current queue, then exit (no `--watch`, no autonomy loop): run `dispatch_runner.py <project> --repo <PROJECT_DIR> --jobs 9 --workers` (no `--watch`) and report the summary.
