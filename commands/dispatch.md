---
description: Watch + drain the DAG review dashboard's task queue — each dropped Worker proves its node via the prover MCP (prove_node, backend-selected), each Reviewer via the jury subagents; keeps the live feed + verdicts in sync. The Claude Code session IS the orchestrator (no separate coordinator). Defaults to --watch on the running dashboard's project.
argument-hint: "[<review-project-dir>] [--once] [--backend max|aristotle|codex] [--dry-run] [--max N]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, Skill
---

# /autoform:dispatch — the dashboard's executor

The DAG review dashboard (`scripts/review_ui/serve_review.py`) is a **dispatcher**: a Worker/Reviewer
drop writes a `queued` task to `task_queue.json`, but the server never runs an agent. This command is
the **executor**. On niket/dev there is **no separate coordinator process** — **you, the Claude Code
session, ARE the orchestrator**: you drain the queue, run each task through the right *native*
mechanism, and keep the dashboard's live feed (`agents_status.json`) + verdicts (`review_status.json`)
in sync. Arguments: `$ARGUMENTS`.

## ⛔ You are a DISPATCHER — delegate every task; never do the work yourself

This is the rule that matters, and the #1 failure mode: **do NOT make a TODO list and work the queue yourself.** You have no authority to score or prove a node — every task is delegated. For EACH task you MUST spawn subagents with the **Task tool** (or call the `prove_node` MCP). The orchestrator's own job is only claim → route → record; it never reads a `.lean` file to judge or edits one to prove.

- **reviewer** → **launch the deterministic watcher in the BACKGROUND**, then return. It drains every queued reviewer node in parallel (each node's 3-judge jury as concurrent `claude -p` processes), writes the verdicts itself, and keeps polling for new dashboard drops — you never score anything. Launch detached (idempotent: skip if one's already watching this project), and report the PID:
  ```
  pgrep -f "dispatch_runner.py.*--watch" >/dev/null \
    && echo "a --watch watcher is already running (pkill -f dispatch_runner.py to stop)" \
    || { nohup env -u ANTHROPIC_API_KEY python3 -u ${CLAUDE_PLUGIN_ROOT}/scripts/dispatch_runner.py <project> --repo <PROJECT_DIR> --jobs 9 --watch >> <project>/dispatch.log 2>&1 & echo "started watcher PID $! — drops now auto-fire"; }
  ```
  **NEVER run `--watch` in the foreground** — it loops forever and would hang this command. Tell the user: drops auto-fire; `tail -f <project>/dispatch.log` to watch; `pkill -f dispatch_runner.py` to stop. (For a one-shot drain instead, run it foreground **without** `--watch`, or with `--once`.)
- **worker** → call the **`prove_node`** MCP tool (or `Task subagent_type:"autoform:autoform-worker"`).
- **planner** → `Task subagent_type:"autoform:splitter"` (or the `plan` skill) over the node's scope.

If you catch yourself opening a `.lean` file to judge it, **STOP** — that's a subagent's job.

**Run tasks in parallel, not one at a time.** The jury judges are READ-ONLY, so dispatch **several reviewer tasks at once** — spawn multiple nodes' juries together (keep ~3–4 nodes × 3 judges in flight, one Task batch per message). Only **workers** serialize, and only when their target files overlap.

Two native mechanisms (no legacy Python engine):
- **Proving** → the unified **prover MCP** `prove_node(node, backend)` (the `autoform-prover` server) —
  its driver + steerer + adapter prove the node on the selected backend.
- **Reviewing** → the **deterministic runner** `scripts/dispatch_runner.py` — it fans every queued
  reviewer node's 3-judge jury out as parallel `claude -p` processes (Max) and writes the verdicts
  atomically. No model-driven delegation, no one-at-a-time.

Load the **formalization-workflow**, **eval-rubrics**, and **lean-conventions** skills first.

## Cost policy — the backend IS the billing path
There is no separate billing switch: the **selected backend determines billing**. `max` → the Max
subscription (the prover's claude adapter runs `claude -p` with `ANTHROPIC_API_KEY` scrubbed);
`aristotle` → Harmonic's `ARISTOTLE_API_KEY`; `codex` → its own. Never let `ANTHROPIC_API_KEY` leak
into a subprocess you spawn (`env -u ANTHROPIC_API_KEY …`).

## Setup
1. **Resolve the review project dir** (holds `graph.json` + `task_queue.json`): an explicit
   `$ARGUMENTS` path > `$AUTOFORM_DISPATCH_PROJECT` > the **running dashboard**
   (`ps -axww -o command | grep '[s]erve_review.py'` → its `--graph` path's parent) > single-dir
   auto-detect > ask. **Echo it.** Note `graph.json`'s `metadata.lean_root` — the Lean project the
   proofs land in; call it **PROJECT_DIR**.
2. Read `graph.json` (nodes + `lean_root`), `review_status.json`, `task_queue.json`. The queue helper
   is `scripts/dispatch_queue.py` (run via `env -u ANTHROPIC_API_KEY python3 scripts/dispatch_queue.py
   <project> …`).
3. **Resolve the backend**: `--backend` > `python3 scripts/backend_config.py get` (set via
   **`/autoform:set-backend`**) > `max`. Map it to the `prove_node` adapter id with
   `python3 scripts/backend_config.py prover` (`max → claude`, `aristotle → aristotle`). **Echo both.**
   Requires the **`autoform-prover` MCP server** (the prover PR) to be registered in `.mcp.json`.
4. **Modes:** `--watch` is the DEFAULT (re-poll ~every 10s for new drops, until interrupted); `--once`
   = a single drain then exit; `--dry-run` = print `node → lean target → mechanism` and stop; `--max N`
   = stop after N tasks. Wait between polls **without foreground-sleeping**.

## The drain loop
Drain every `queued` task; in `--watch`, re-poll for new drops (until interrupted / `--max`). Each task:

1. **Claim:** `dispatch_queue.py <project> next` → `{id, agent, node, node_label}`. Mark running + light
   the feed: `dispatch_queue.py <project> claim <id> --detail "…"`. The node pulses on the dashboard.
2. **Resolve the node's Lean target** from `graph.json` (a `module` node → `<lean_root>/<id with
   '.'→'/'>.lean`, or its explicit `lean_file`). Echo it.
3. **Run the native mechanism:**
   - **worker** → call the **`prove_node` tool from the `autoform-prover` MCP** with
     `{graph_path: "<project>/graph.json", node_id: "<node>", project_dir: "<PROJECT_DIR>",
     backend: "<prover id>"}`. The prover's driver + steerer + adapter prove the node on the selected
     backend; its claude adapter already carries the no-cheating / honest-`FAILED` worker discipline
     and the **Phase-0 lakefile precondition**. Read the returned `{status, reason}`.
   - **reviewer** → **not handled per-task.** The **background watcher** launched at the start
     (`dispatch_runner.py --watch`) drains every queued reviewer node in parallel (3 judges each),
     applies the threshold gate (**rejected** if faithfulness ≤ 2 or proof_integrity ≤ 2; **clean** if
     faithfulness ≥ 4 ∧ proof_integrity ≥ 3 ∧ code_quality ≥ 3; else **flagged**), writes each `ai` slot
     (`source:"dispatch:runner"`, preserving any `human` slot, atomic), and keeps doing so for new
     drops. The per-task steps here apply only to **worker / planner** tasks.
   - **planner** → `Task subagent_type:"autoform:splitter"` (or the `plan` skill) over the node's scope → produce/refresh the sub-DAG.
4. **Finish:** only on a real, verified result — `dispatch_queue.py <project> done <id> --result "…"`.
   On an honest failure (prove_node `status: failed`, an unbuildable project, a split/negative jury that
   isn't a clean pass), `… fail <id> --reason "<blocker>"`. The helper idles the feed when nothing is
   running.

## Honesty (non-negotiable)
- A **reviewer** task records the jury's *actual* verdict — never `clean` without the evidence.
- A **worker** task is `done` ONLY if `prove_node` returns `proved` (the `sorry` is gone, build clean,
  no `sorryAx`); otherwise `failed` with the concrete blocker. Never fake a proof.
- The feed mirrors exactly what's running — never fabricate a `running`/`done`. Process **worker** tasks
  one at a time unless their files are disjoint; **reviewer** tasks run concurrently — keep several
  nodes' juries (each 3 read-only judges) in flight at once.

## Close-out
On exit (`--once` done, `--max` hit, or interrupt), idle the feed (`dispatch_queue.py <project> idle`)
and print a summary table: `task | node | agent | backend | outcome`. In `--watch`, print a running
tally each time the queue drains to empty before going back to polling.
