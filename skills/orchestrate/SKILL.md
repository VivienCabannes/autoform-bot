---
name: orchestrate
description: >-
  Run or resume Autoform's durable formalization workflow: launch the
  deterministic jury/prover engine, drive native planning and review subagents,
  coordinate proof recovery, and advance the dependency graph to a clean trust
  frontier. Use when the user asks to orchestrate, run, resume, automate, prove,
  review, score, choose or inspect a prover backend, search Lean Zulip for prior
  art, inspect progress, or finish an Autoform plan.
---

# Orchestrate Autoform

Operate one durable queue with two cooperating actors:

- `scripts/dispatch_runner.py` deterministically drains `reviewer` and `worker`
  tasks.
- The current interactive host drains planning, review, and proof-recovery tasks with
  native subagents.

Do not review or prove a queued Lean node in the parent session. Do not shell out
to a second agent host to emulate delegation.

## Resolve paths and configuration

Resolve one absolute plugin root from a valid `AUTOFORM_PLUGIN_ROOT`,
`MUSE_PLUGIN_ROOT`, `PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
`Path(<this loaded SKILL.md>).resolve().parents[2]`. Validate that it contains
`scripts/dispatch_runner.py` and `internal/runbooks/proving.md`. Substitute that
quoted absolute path into every command; do not rely on shell state from an
earlier tool call.

Before proof or review work, load the internal operating material rather than
expecting another user command:

- proof work: `internal/runbooks/proving.md` and
  `internal/runbooks/mathlib-style.md`;
- jury or human review: `internal/runbooks/evaluation.md` and
  `internal/runbooks/review.md`;
- community prior-art search: `internal/runbooks/zulip.md`;
- multi-machine/team progress: `internal/runbooks/worker.md`.

Those runbooks and their references are implementation details of Orchestrate.

Orchestrate also owns prover-backend selection. When the user asks which
backends are available or which default is active, run:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/backend_config.py" list
```

When the user explicitly asks to change the persistent default, run:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/backend_config.py" set <backend>
```

Report the selected adapter, host authentication or credential variable,
billing path, and whether project content may leave the machine. An explicit
backend requested for the current run overrides the persisted default, but does
not change it unless the user asks to persist the choice. Never infer an unknown
backend as Claude; let validation fail closed.

Resolve:

- dispatch project: explicit argument, then `AUTOFORM_DISPATCH_PROJECT`, then a
  running dashboard's graph parent;
- Lean project: `graph.json` metadata `lean_root`, otherwise the dispatch
  project's repository parent;
- proof backend: explicit choice for this run, otherwise run `backend_config.py get
  --fallback codex` on Codex, `--fallback max` on Claude, or `--fallback muse`
  on Muse. A persisted choice wins over the host fallback;
- judge backend: explicit argument, otherwise `AUTOFORM_JUDGE_BACKEND`, otherwise
  the host-native CLI (`claude` on Claude Code, `codex` on Codex, `muse` on Muse).

Echo all four before claiming work. Preflight the required host CLI. In
particular, a persisted `max` choice on Codex or Muse still requires `claude`;
if it is not installed/authenticated, stop and ask the user to select another
available backend or install Claude. The `muse` prover or judge requires the
`tbh` CLI and its configured provider authentication. Never silently override a
resolved choice.

For every distinct API provider (`openai` or `avocado`) used by either prover or
judge, run the local configuration check before launching:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/provider_check.py" <provider>
```

Before a real API workload—regardless of a globally persisted backend—tell the
user the provider, base URL host, and that source excerpts, project files, Lean
code, build/search output, and jury prompts may leave the machine. Obtain
explicit approval for this project/run. Check each distinct provider; approval
for one does not approve another. If the user separately authorizes a minimal
network probe, add `--live`; the probe sends only a generated marker from a
temporary directory. Do not claim queue work when consent, configuration, or
tool calling is missing.

After approval, add one `--allow-api-egress <provider>` flag to the dispatcher
command for every distinct approved API provider. The dispatcher refuses to
start without these per-process confirmations; persisting a backend or running
a capability probe never supplies them.

## Launch the engine

Unless a one-shot run was requested, launch one detached watch process for this
exact dispatch project:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" \
  python -u "<AUTOFORM_PLUGIN_ROOT>/scripts/dispatch_runner.py" "$DISPATCH_PROJECT" \
  --repo "$LEAN_PROJECT" --backend "$PROVER_BACKEND" \
  --judge-backend "$JUDGE_BACKEND" <APPROVED_API_EGRESS_FLAGS> \
  --jobs 9 --watch --workers
```

Log to `<dispatch-project>/dispatch.log`. Reuse an existing matching watch
process. Never run watch mode in the foreground.
Replace `<APPROVED_API_EGRESS_FLAGS>` with the approved repeated flags, or omit
the placeholder entirely when neither selected backend is an API provider.

For a one-shot run, omit `--watch` and wait for the process to exit.

## Distributed mode (shared GitHub roadmap)

When the Lean project has a GitHub `origin` remote and the user wants
multi-machine or team progress, route cross-machine coordination through the
worker CLI (`python -m autoform_worker`, run through the plugin root) and
follow `internal/runbooks/worker.md`:

1. Preflight once per session: `python -m autoform_worker doctor --json`.
   Surface failing checks to the user before starting.
2. Converge local review state first: `python -m autoform_worker sync
   --project "$DISPATCH_PROJECT"` (folds merged PRs' scoreboards into the local
   sidecar; idempotent).
3. Before enqueueing a `worker` task or proving any node, check and take the
   cooperative lease: `autoform claim acquire --node "<node id>"`. "held by a
   live peer" means pick different work — never race a peer on a node. Release
   the lease when the attempt ends.
4. For autonomous distributed progress, prefer one detached
   `python -m autoform_worker work --loop --project "$DISPATCH_PROJECT"`
   (log to `worker.log`, reuse an existing loop) over hand-driving rounds. The
   loop is not only a prover: its `agents` stage drains every queue kind the
   role registry knows — planner, mathcheck, graphreview, contentreview,
   counterexample, priorart, holistic, escalation, and any project-local role —
   by spawning that role's own instructions. The local engine and dashboard
   continue to run exactly as below; the worker adds PR/claim/scoreboard
   coordination on top and does not replace the queue.
   `python -m autoform_worker agents` lists the registered roles. To add an
   agent type, write `agents/<kind>.md` (or
   `<project>/.autoform/agents/<kind>.md`) — the palette, the queue, and the
   loop all derive from those files; never hardcode a new kind.
5. Surface `python -m autoform_worker status` in progress reports (open PRs by
   stage, live claims, suppressed candidates and why).

In distributed mode a proof lands as a pull request with an
`autoform-target:v1` marker and gets its jury verdict as a scoreboard comment
on the PR; the committed sidecar is updated by the worker's `progress` unit
after merge. Local-only projects (no remote): skip this section entirely.

Humans steer through the two dashboards, not through merge buttons: a PR
auto-merges once CI is green and a trusted `clean` jury verdict exists at its
head, while a human `flagged`/`rejected` verdict recorded in the review
dashboard holds the gate for that node. Report progress in those terms — what
the roadmap site will show — rather than asking the user to merge.

## Queue ownership

| Kind | Owner | Action |
|---|---|---|
| `reviewer` | engine | Parallel rubric jury; writes only the AI review slot. |
| `worker` | engine | Proves through the selected adapter and shared kernel gate. |
| `escalation` | interactive host | Run ordered proof recovery after a failed attempt. |
| `mathcheck` | interactive host | Spawn native `mathlib-checker`. |
| `graphreview` | interactive host | Spawn native `graph-reviewer`. |
| `contentreview` | interactive host | Spawn native `content-reviewer`. |
| `holistic` | interactive host | Spawn at least three independent native `holistic-reviewer` agents. |
| `planner` | interactive host | Run the cluster split/check/review pipeline. |

Use `scripts/dispatch_queue.py <project> mine` as the complete interactive-host
worklist. Every owned task follows `claim` → do the work → `done`, or `fail`
with a concrete reason. Never wait for the engine to close an interactive-host
task.

## Native role mapping

Use the host's native parallel subagent interface and these canonical roles:

- `autoform_reader`
- `autoform_splitter`
- `autoform_mathlib_checker`
- `autoform_graph_reviewer`
- `autoform_content_reviewer`
- `autoform_holistic_reviewer`
- `autoform_source_searcher`
- `autoform_proof_strategy_researcher`

Claude may display canonical hyphenated plugin role names. Codex project TOMLs
use the namespaced underscore names above. If Codex role files are missing, run:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/install_host_agents.py" install \
  --host codex --project "$LEAN_PROJECT"
```

Codex native spawn tools do not all expose a custom-role selector, and role
files installed during the current task may not be loaded until a new
project-rooted task starts. In either case, use a generic native Codex subagent
and paste the complete canonical `agents/<role>.md` instructions into its task.
This prompt-inlining fallback is mandatory; never substitute a bare generic
role description.
On Muse, always use the generic native subagent interface and this same full-body
prompt-inlining fallback because the public Muse plugin manifest does not expose
an `agents` capability. Pass the absolute plugin root and project paths in the
task.
For `lean-lsp-mcp` calls, pass absolute project file paths when practical. Use
Loogle, LeanExplore, and `scripts/mathlib_search.py` for stateless Mathlib search.

## Planner pipeline

For a tier-1 cluster:

1. Claim the planner task.
2. Spawn the canonical `splitter` role with the cluster, sources, existing prerequisite node index,
   and output paths.
3. Upsert returned node records through `scripts/merge_node.py`.
4. Spawn one canonical `mathlib-checker` per new tier-2 node in parallel; merge its
   structured result through `merge_node.py`.
5. Spawn canonical `graph-reviewer` and `content-reviewer` roles in parallel over the cluster.
   Route structural edits through `merge_node.py`; the content role owns only its
   prose files.
6. Enqueue jury review for the new nodes.
7. Mark the planner task done only after all earlier steps have durable output.

## Proof recovery pipeline

The queue keeps the historical kind name `escalation` for compatibility, but
it means proof recovery. Claim the task and run these waves in order:

1. Spawn at least two independent `proof-strategy-researcher` agents, plus
   prior-art and source search. Require a complete informal route tied to exact
   Mathlib declarations or named intermediate claims.
2. If no viable proof route survives comparison, spawn at least two independent
   counterexample hunters. A verified disproof stops proof attempts until the
   statement is corrected; park the recovery in the meantime.
3. If neither proof nor disproof succeeds, spawn independent decomposition
   agents. Accept sublemmas only with an explicit reconstruction of the target,
   then add them through `merge_node.py` and schedule them foundations-first.
4. If the decomposition is not viable, broaden exploration with distinct
   mathematical methods and sources. Record checked routes so later recovery
   work does not repeat them.

Write the selected route, counterexample, or reconstruction into the node's
`## Proof recovery` ledger. Mark the recovery `done` and enqueue a worker only
after a durable input or strategy change. If a complete wave finds no defensible
next route, use `dispatch_queue.py <project> park <task-id> --reason <evidence>`.
A parked recovery stays visible and can be resumed with `resume <task-id>` when
new evidence appears. It is not a request for human proof work.

## Pipeline position

Publish ``--stage prove`` (via ``dispatch_queue.py <project> orchestrator``)
while the engine/worker rounds run, and ``--stage publish`` during progress
folds and site publication, so the dashboard stepper tracks the run. Keep
``--phase``/``--detail`` in plain language.

## Autonomy loop

Unless manual/drop-only mode was requested:

1. Read graph, reviews, queue, and open proof recoveries.
2. Drain interactive-host work first, proof recoveries first.
3. Traverse foundations-first (with declared targets, critical path first):
   - roadmap gaps → run `scripts/roadmap_audit.py "$DISPATCH_PROJECT/graph.json"
     --enqueue` once per pass; it turns every completeness failure (status,
     grounding, unverified in-Mathlib claims, missing prose, target
     reachability) into a queued role task the loop drains;
   - unreviewed node → enqueue `reviewer`;
   - defective or unproved node with clean prerequisites and no open recovery
     → enqueue `worker`;
   - unsplit tier-1 cluster → planner pipeline;
   - stale or guessed Mathlib status → `mathcheck`.
4. Enqueue a bounded wave. Queue operations deduplicate; never double-run engine
   tasks as subagents.
5. Poll the queue and log. As soon as `mine` is non-empty, stop waiting and
   handle it.
6. Continue until the trust frontier is clean, the explicit task limit is hit,
   or every remaining theorem has a parked recovery with a durable evidence
   ledger.

Proof recovery may add a real missing prerequisite through `merge_node.py`,
start a cluster-level planner pass, or establish a false statement or toolchain
failure. Do not grow the DAG repeatedly to disguise an unresolved proof. The
engine has no arbitrary attempt cap; its fingerprint gate blocks unchanged
retries.

## Completion

Before reporting completion:

- `dispatch_queue.py <project> mine` is empty;
- every claimed task is terminal or explicitly parked;
- no proof was accepted without the shared verification gate;
- the activity feed mirrors actual state;
- the remaining parked frontier and provider-specific failures are explicit.

When the user asks for a node packet, review UI, or a fresh score without a
full autonomous run, follow `internal/runbooks/review.md` directly and perform
only that bounded review operation.
