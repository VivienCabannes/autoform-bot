---
name: orchestrate
description: >-
  Run or resume Autoform's durable formalization workflow: launch the
  deterministic jury/prover engine, drive native planning and review subagents,
  triage escalations, and advance the dependency graph to a clean trust
  frontier. Use when the user asks to orchestrate, run, resume, automate, prove,
  review, score, search Lean Zulip for prior art, inspect progress, or finish an
  Autoform plan.
---

# Orchestrate Autoform

Operate one durable queue with two cooperating actors:

- `scripts/dispatch_runner.py` deterministically drains `reviewer` and `worker`
  tasks.
- The current interactive host drains planning/review/escalation tasks with
  native subagents.

Do not review or prove a queued Lean node in the parent session. Do not shell out
to a second agent host to emulate delegation.

## Resolve paths and configuration

Resolve one absolute plugin root from a valid `AUTOFORM_PLUGIN_ROOT`,
`PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
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
- community prior-art search: `internal/runbooks/zulip.md`.

Those runbooks and their references are implementation details of Orchestrate.

Resolve:

- dispatch project: explicit argument, then `AUTOFORM_DISPATCH_PROJECT`, then a
  running dashboard's graph parent;
- Lean project: `graph.json` metadata `lean_root`, otherwise the dispatch
  project's repository parent;
- proof backend: explicit argument, otherwise run `backend_config.py get
  --fallback codex` on Codex or `--fallback max` on Claude. A persisted choice
  still wins;
- judge backend: explicit argument, otherwise `AUTOFORM_JUDGE_BACKEND`, otherwise
  the host-native CLI (`claude` on Claude Code, `codex` on Codex).

Echo all four before claiming work. Preflight the required host CLI. In
particular, a persisted `max` choice on Codex still requires `claude`; if it is
not installed/authenticated, stop and ask the user to select `codex` or install
Claude. Never silently override a persisted choice.

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
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" --extra aristotle \
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

## Queue ownership

| Kind | Owner | Action |
|---|---|---|
| `reviewer` | engine | Parallel rubric jury; writes only the AI review slot. |
| `worker` | engine | Proves through the selected adapter and shared kernel gate. |
| `escalation` | interactive host | Triage a worker's concrete blocker. |
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
role description. Pass the absolute plugin root and project paths in the task.
For Mathlib MCP calls, pass `project_dir` explicitly when the tool accepts it.

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

## Autonomy loop

Unless manual/drop-only mode was requested:

1. Read graph, reviews, queue, and open escalations.
2. Drain interactive-host work first, escalations first.
3. Traverse foundations-first:
   - unreviewed node → enqueue `reviewer`;
   - defective or unproved node with clean prerequisites and no open escalation
     → enqueue `worker`;
   - unsplit tier-1 cluster → planner pipeline;
   - stale or guessed Mathlib status → `mathcheck`.
4. Enqueue a bounded wave. Queue operations deduplicate; never double-run engine
   tasks as subagents.
5. Poll the queue and log. As soon as `mine` is non-empty, stop waiting and
   handle it.
6. Continue until the trust frontier is clean, the explicit task limit is hit,
   or a blocker genuinely requires the user.

An escalation may add a real missing prerequisite through `merge_node.py`, start
a cluster-level planner pass, or surface a false statement/toolchain failure.
Do not grow the DAG repeatedly to disguise an unresolved proof. Respect the
engine's escalation cap.

## Completion

Before reporting completion:

- `dispatch_queue.py <project> mine` is empty;
- every claimed task is terminal;
- no proof was accepted without the shared verification gate;
- the activity feed mirrors actual state;
- remaining blocked frontier and provider-specific failures are explicit.

When the user asks for a node packet, review UI, or a fresh score without a
full autonomous run, follow `internal/runbooks/review.md` directly and perform
only that bounded review operation.
