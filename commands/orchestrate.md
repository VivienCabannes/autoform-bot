---
description: The autoform orchestrator — launch the deterministic dispatch engine (parallel review jury + prover workers) and, by default, autonomously drive the formalization to a clean trust frontier; or let the human drive it via the dashboard, or both, off one shared queue.
argument-hint: "[<review-project-dir>] [--manual] [--max-tasks N] [--backend max|aristotle|codex] [--once]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, Skill
---

# /autoform:orchestrate — the dispatch engine + autonomous driver

Two things run, sharing **one task queue** (`task_queue.json`, the file the dashboard writes when a human drops an agent on a node):

- a **deterministic background engine** — `scripts/dispatch_runner.py --watch --workers` — that drains the queue continuously: **reviewers** as a parallel 3-judge jury, **workers** via the prover core, billed to Max (key scrubbed). It handles human drops AND the tasks you queue, idempotently.
- **you**, the orchestrator — who (by default) inspect the graph, **self-queue** the next work foundations-first, and run the **planning/review agents** (splitter · graph/content/holistic reviewers · mathlib-checker) as `Task` subagents, merging any graph change through `merge_node.py`.

So it runs **autonomously**, the **human drives** it via the dashboard, or **both**. Arguments: `$ARGUMENTS`.

## ⛔ You never review or prove a node yourself
Reviewing and proving are the engine's job (deterministic, parallel). You only: launch the engine, decide what to queue, `enqueue` it, **drain your own queue** — `claim` → run → `done` each of your six kinds (escalation, planner, the three reviews, mathcheck) — and report. If you catch yourself scoring a node or editing a `.lean` proof, STOP — that's the engine.

## Setup
1. **Project dir** (holds `graph.json` + `task_queue.json`): explicit `$ARGUMENTS` > `$AUTOFORM_DISPATCH_PROJECT` > the running dashboard (`ps -axww -o command | grep '[s]erve_review.py'` → its `--graph` parent) > ask. **Echo it.** Note `metadata.lean_root` → **PROJECT_DIR** (where proofs land).
2. **Backend**: `--backend` > `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/backend_config.py get` > `max`. **Echo it.**

## 1. Launch the engine (always)
Detached + idempotent — skip if a `--watch` engine is already up. Launched via `uv run
--extra aristotle` so the chosen backend's deps (aristotlelib) are present; harmless for
`max`/`codex`. Pass the resolved `$BACKEND` through with `--backend`:
```
pgrep -f "dispatch_runner.py.*--watch" >/dev/null \
  && echo "engine already running (pkill -f dispatch_runner.py to stop)" \
  || { nohup env -u ANTHROPIC_API_KEY uv run --directory ${CLAUDE_PLUGIN_ROOT} --extra aristotle \
        python -u ${CLAUDE_PLUGIN_ROOT}/scripts/dispatch_runner.py <project> --repo <PROJECT_DIR> \
        --backend $BACKEND --jobs 9 --watch --workers >> <project>/dispatch.log 2>&1 & echo "started engine PID $!"; }
```
`--workers` proves nodes autonomously on `$BACKEND` (claude = headless Max worker with
skip-permissions; aristotle = Harmonic; codex = its own auth) — and every claimed proof
passes the shared **verification gate** (`servers/prover/verify.py`: builds clean + `#print
axioms` shows no `sorryAx`) before it can land. The 3-judge jury always bills Max (key
scrubbed). **NEVER run a `--watch` in the foreground** — it loops forever and hangs this
command. Tell the user: drops auto-fire; `tail -f <project>/dispatch.log` to watch; `pkill
-f dispatch_runner.py` to stop.

## 2. The dispatch kinds (the dashboard palette + queue)
Two kinds are drained by the **deterministic engine** — only ever `enqueue` them, never `Task` them (that double-runs and breaks the engine's honesty guarantee). Five **you** run as `Task` subagents, routing every graph change through `scripts/merge_node.py` — the **sole graph writer**; subagents return data, they never write `graph.json` themselves. The last, **`escalation`**, the engine *raises* when a worker hits a wall and **you triage** — the worker's "I'm stuck, here's what's missing" signal, not a subagent.

**All six of your kinds share one lifecycle: `claim` the queued task → run its subagent(s)/pipeline (graph edits via `merge_node.py`) → mark it `done` (or `fail` if dismissed).** The engine closes none of them, so any you leave `queued` dangles forever — `claim`/run alone does **not** resolve it. `dispatch_queue.py <project> mine` is your full worklist (every open `escalation`/`planner`/`graphreview`/`contentreview`/`holistic`/`mathcheck`); it must be empty before a run ends.

| kind | who | action |
|---|---|---|
| **reviewer** | engine | `dispatch_queue.py <project> enqueue --agent reviewer --node <id>` → the parallel 3-judge jury writes the `ai` verdict. |
| **worker** | engine | `… enqueue --agent worker --node <id>` → the prover fills the node; `done` only on a real `proved`. On a FAILED it auto-raises an `escalation` (below) carrying the worker's words. |
| **escalation** | engine → you | auto-raised when a worker FAILs; the queue entry carries the worker's own words in `note`. It sits `queued` but **the engine never drains it** — it looks like engine work and isn't; only you clear it. List every open one with `dispatch_queue.py <project> escalations` (full notes). `claim` it, then **triage with judgment**: dedup the named gap against existing nodes; a genuine new prerequisite → `merge_node.py` (add a `missing` node + an edge into the blocked node) then `enqueue reviewer`/`worker` for it; a **cluster-level** gap → run the `planner` pipeline; a non-DAG failure (toolchain / false statement / honest give-up) → **don't grow the DAG**, surface to the user; then `done` it (or `fail` with a one-line reason if dismissed). A human may also drop it to ask you to look at a node. |
| **mathcheck** | you | `claim` → `Task autoform:mathlib-checker` with `{name, kind, description}` → merge the returned `{mathlib_status, mathlib_declarations, mathlib_file, mathlib_notes}` into the node via `merge_node.py` → `done` the task. |
| **graphreview** | you | `claim` → `Task autoform:graph-reviewer` (opus) with the node-id partition + `graph.json` path + `merge_node.py` path + tier/phase → apply high-confidence edge payloads via `merge_node.py`; surface uncertain ones → `done` the task. |
| **contentreview** | you | `claim` → `Task autoform:content-reviewer` with the cluster's tier-2 ids, their `informal_content/<id>.md` paths + structural fields, and source paths → it edits the `.md` files directly; route any structural flag to a `graphreview` → `done` the task. |
| **holistic** | you | graph-scoped: `claim` → launch **≥3** `Task autoform:holistic-reviewer` in parallel over the **whole** graph → apply small fixes via `merge_node.py`, re-dispatch large ones as a targeted `graphreview` → `done` the task. |
| **planner** | you | the full split → check → review pipeline (below). |

**Scope — dereference before invoking.** Queue entries are per-node (`{agent, node}`). `reviewer`/`worker`/`mathcheck` act on that one node. `planner`/`graphreview`/`contentreview` act on a **tier-1 cluster** — the entry's `node` is the cluster id; read `graph.json` and resolve it to its child tier-2 ids first. `holistic` acts on the **whole graph** — ignore the dropped node, pass the full node set. Human-in-the-loop gate for every graph edit: high-confidence → apply via `merge_node.py`; uncertain/conflicting → surface to the user with the reviewer's reasoning; rejected → compensating `merge_node.py`.

### The `planner` pipeline (one drop = a fully-reviewed sub-DAG)
On a `planner` task for tier-1 cluster **C** — replaces the old bare-splitter behavior. **`claim` the planner task first**, then:
1. **Split** — `Task autoform:splitter` with C's id/description/source_refs/provisional_members, the source paths, and a trimmed index of prerequisite tier-2 ids + green Mathlib roots. It writes `informal_content/<id>.md` and returns the tier-2 node records.
2. **Merge** — upsert the returned records into `graph.json` via `merge_node.py` (it strips edges to removed nodes). Note any tier-1 flags for the user.
3. **Mathlib fan-out** — for each new tier-2 node, `Task autoform:mathlib-checker` (in parallel) → merge each `{mathlib_status, …}` via `merge_node.py`, overwriting the splitter's provisional guesses.
4. **Review wave** — one `Task autoform:graph-reviewer` (tier 2) + one `Task autoform:content-reviewer` over C's new tier-2 set; apply graph payloads via `merge_node.py`, content-reviewer edits prose directly.
5. **Hand off** — the new nodes are now unreviewed frontier; fall into the loop below and `enqueue reviewer` (then `worker` once prerequisites are clean) — the engine takes them.
6. **Done** — `dispatch_queue.py <project> done <planner-id>`. The task closes only here, once its sub-DAG is merged, mathlib-checked, reviewed, and handed to the loop — never before. Leaving it `queued` is what makes a planner "never resolve".

## 3. The autonomy loop — default FULL-AUTO (`--manual` = drop-only)
Unless `--manual`, loop until every target's closure is clean or `--max-tasks` (default 40) is hit:
1. **Read** `graph.json` + `review_status.json` + the queue. **Drain your own queued work first** — `dispatch_queue.py <project> mine` lists every open orchestrator-owned task (escalation/planner/graphreview/contentreview/holistic/mathcheck; escalations show their full `note`). **None of these is engine work** — the engine never closes them, so each dangles until you `claim` → run it (per the table / planner pipeline) → `done` it (or `fail`). Do escalations first (they block proving), then the rest. Then, foundations-first (topological by `depends_on`): **unreviewed** node → `enqueue reviewer`; **defective/unproven** (`rejected`/`flagged`/`sorry`/`missing`, prerequisites clean **and no open `escalation` on the node**) → `enqueue worker`; a **coarse cluster with no fine children** → run the `planner` pipeline; a node with a **guessed/stale `mathlib_status`** → `mathcheck`. The engine enforces these bounds too — it skips a worker whose node has an open escalation, and after `--max-escalations` (default 3) rounds it marks the worker `blocked: … needs human` and stops re-proving, so a hard node can't loop. If a node keeps re-escalating, **stop growing the DAG and surface it to the user** rather than re-queueing.
2. **Enqueue a bounded wave** (≈ `--jobs`×2 reviewers + a few workers) via `dispatch_queue.py <project> enqueue --agent <kind> --node <id>`; it dedups — skip nodes already `running`; don't queue a worker whose prerequisites are still `rejected`.
3. **Run your kinds** — for each orchestrator-owned task in `mine` (planner/graphreview/contentreview/holistic/mathcheck + any escalation): `claim` it, run it per the table/pipeline, then `done` it. The engine drains reviewer/worker; it never closes yours.
4. **Wait**: poll `dispatch_queue.py <project> status` (+ `dispatch.log`) until the wave reaches `done`/`failed` — but **a queued orchestrator-owned task never reaches `done` on its own** (the engine drains only reviewer/worker), so the instant `status` banners one (it prints `⚑⚑ … AWAIT THE ORCHESTRATOR` at the top) or `mine` is non-empty, **stop waiting and handle it now** instead of polling for it to clear. Then **re-read and queue the next** — each clean proof unblocks dependents, each clean review exposes the next frontier.
5. **Stop** at a clean frontier or `--max-tasks` — but **never while any orchestrator-owned task is still open**: `dispatch_queue.py <project> mine` must come back empty (every escalation/planner/graphreview/contentreview/holistic/mathcheck claimed → run → `done`, or surfaced to the user) before you declare done, else that work is silently abandoned. Summarize verdicts + the remaining frontier. Optionally run one `holistic` triplet over the whole graph before declaring done.

The human can drop any of the eight kinds in the dashboard at any time — they land in the same queue; the engine drains its two (and auto-raises `escalation`); you drain the other six — `claim` → run → `done`, with `dispatch_queue.py <project> mine` as your full worklist. Autonomous and manual coexist.

## Honesty (non-negotiable)
- The engine records the jury's **actual** verdict and marks a worker `done` ONLY on a real `proved` (sorry gone, build clean, no `sorryAx`) — never a faked proof. Don't override it.
- The live feed mirrors real state; never fabricate `running`/`done`.

## --once
A single foreground drain of the current queue, then exit (no `--watch`, no autonomy loop): run `dispatch_runner.py <project> --repo <PROJECT_DIR> --jobs 9 --workers` (no `--watch`) and report the summary.
