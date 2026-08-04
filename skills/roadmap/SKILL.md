---
name: roadmap
description: >-
  Build, resume, review, reset, or visualize the informal formalization roadmap
  — the tiered dependency DAG (graph.json + per-node prose) an AutoformBot
  fleet proves against. Use for plan a formalization, build or grow the
  roadmap/DAG, scope sources or chapters, split clusters, re-plan, reset the
  plan, view the graph, or render the mathematical blueprint. Repository and
  environment readiness belong to Setup; proving belongs to Orchestrate.
---

# Build the formalization roadmap

Turn confirmed informal sources into the durable, reviewed dependency graph
that provers and reviewers drain. Roadmap owns the *plan*: sources and scope,
the tiered DAG, per-node prose, Mathlib status, and graph visualization. It
assumes a Setup-ready project (Lean repository, durable state, dashboard) and
never proves anything — proving and reviewing are Orchestrate's.

## Resolve the plugin root

Resolve one absolute plugin root. Prefer a valid `AUTOFORM_PLUGIN_ROOT`,
`MUSE_PLUGIN_ROOT`, `PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
`Path(<this loaded SKILL.md>).resolve().parents[2]`. The result must contain
`scripts/merge_node.py` and `internal/runbooks/planning.md`; stop if it does
not. In every command below, replace `<AUTOFORM_PLUGIN_ROOT>` with that quoted
absolute path; do not depend on a variable exported by a previous shell call.

## Start with a run brief

Before reading sources or spawning any subagent, tell the user:

- the resolved Lean repository and plan/roadmap directory;
- whether this is a fresh plan, a resume of a partial graph, or a re-plan;
- the confirmed source files and exact chapter/section scope, or the specific
  missing information you need before planning;
- the artifacts this run will create or update (`graph.json`,
  `informal_content/`, and the live dashboard view);
- the checkpoints: coarse roadmap approval before detailed splitting, and that
  no prover is dispatched from this workflow.

Do not make the user infer whether subagents have started or which files they
may touch. Report those transitions when they occur.

Publish pipeline position to the dashboard feed at every transition —
``dispatch_queue.py <project> orchestrator --state working --stage <s> --phase
"<label>" --detail "<one line>"`` — with ``--stage plan`` during coarse
planning and review waves, ``--stage approve`` while waiting at the
coarse-roadmap checkpoint, and ``--stage split`` during detailed splitting.
The stage drives the dashboard's position stepper; the phase/detail lines are
free text and should stay in plain language a user can read without knowing
the pipeline's internals (say "fixing 2 major findings from the big-picture
reviewers", not internal shorthand).

## Procedure

1. Resolve the project. `DISPATCH_PROJECT` is the directory owning
   `graph.json`; `PROJECT_DIR` is the Lean repository (from `graph.json`
   metadata when present). If there is no Lean project or the environment is
   broken, run Setup first — Roadmap does not install or create repositories.
   If only the durable state is missing, initialize it non-destructively:

   ```bash
   uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/init_plan.py" \
     --project "$DISPATCH_PROJECT" --lean-root "$PROJECT_DIR"
   ```

2. A request to "rebuild" or "re-plan" does not authorize deletion: resume the
   graph and refine it. Only an explicit user-confirmed plan reset authorizes
   adding `--reset-plan`. Before executing it, state that graph, prose, queue,
   reviews, and activity will be reset and that a timestamped snapshot will be
   retained under `<dispatch-project>/.autoform/snapshots/`.

3. Confirm source files and scope with the user before planning. If sources
   the user named are absent from the machine, say exactly what is missing
   before proceeding.

   The roadmap is a UNIFIED ARGUMENT you author, in the leanblueprint sense —
   not a transcription of any one source. Cited material anchors it, but real
   papers have gaps, and bridging them with your own mathematical knowledge is
   part of the job: write the connective statements, mark them `origin:
   bridged`, and let the adversarial reviewers (counterexample, faithfulness,
   content) hold them to the same standard as everything else. Standard
   textbook material may enter as `origin: background`. Two provenance rules
   are absolute: never fabricate a citation (a `cited` node's `source_refs`
   must genuinely support its statement), and never leave a node's origin
   ambiguous — the blueprint must always know which mathematics is recovered
   from the corpus and which is authored.

   Map the whole requested source at roadmap and coverage granularity before
   decomposing theorem nodes, then build the detailed DAG only for the approved
   milestone. Every detailed node must retain a stable `source_refs` anchor.
   The cited source remains authoritative for statement recovery and
   faithfulness review.

4. Ensure the dashboard is visible so the DAG appears while it grows: reuse a
   service already serving this project, otherwise start it exactly as Setup
   does (`scripts/service_control.py start review …`). A dashboard failure
   never blocks planning; report it and continue.

5. Read and follow `<AUTOFORM_PLUGIN_ROOT>/internal/runbooks/planning.md`,
   including its schema at
   `<AUTOFORM_PLUGIN_ROOT>/internal/references/plan-json-schema.md`. Planning
   is incomplete when the graph is absent or empty, a tier-1 cluster has no
   tier-2 children, or a node has null content. Preserve every durable node
   already merged.

   Phase 1 (coarse clusters) ends at a user checkpoint: present the tier-1
   roadmap and get approval before detailed splitting. Phase 2 runs the
   split/check/review waves per cluster with native subagents for the
   canonical roles (`splitter`, `mathlib-checker`, `graph-reviewer`,
   `content-reviewer`, `holistic-reviewer`), and routes every graph edit
   through `scripts/merge_node.py` — it is the only writer of `graph.json`.

6. When the user names a target theorem (a mission sink the fleet should reach),
   record it as first-class graph state through the single writer:

   ```bash
   echo '{"metadata": {"targets": [{"node": "<target-node-id>"}]}}' | \
     uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/merge_node.py" "$DISPATCH_PROJECT/graph.json"
   ```

   Targets drive the workers' prove ordering (critical path first) and the
   audit's reachability clause; the status surfaces report distance to each.

7. Before reporting, audit completeness — structure plus every roadmap clause
   (status vocabulary, grounding, verified in-Mathlib claims, prose, provenance,
   targets, Lean paths):

   ```bash
   uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/roadmap_audit.py" \
     "$DISPATCH_PROJECT/graph.json"
   ```

   A structural failure is this run's to fix, not the next workflow's. Other
   clauses may legitimately have offenders mid-planning; report the counts.

   When a Mathlib checkout is present (`$PROJECT_DIR/.lake/packages/mathlib`,
   or `MATHLIB_PATH`), ALWAYS add `--verify-decls --stamp-verified` — never
   leave in-Mathlib claims unverified. The trust frontier believes an
   `in-mathlib` status by construction, so a single hallucinated declaration
   among unstamped claims poisons everything built on it, and the check is a
   deterministic grep (seconds, no model tokens). Run it as soon as the
   mathlib-check wave has merged its statuses, not only at the end. Skip it
   only when no checkout exists — and say so in the report.

   Add `--enqueue` only when the user wants the remaining gaps turned into
   queued role tasks for Orchestrate/worker rounds to drain.

8. The lightweight dashboard is the default visualization. Only when the user
   explicitly requests the publication-style mathematical blueprint, read and
   follow `<AUTOFORM_PLUGIN_ROOT>/internal/runbooks/visualization.md` to
   export, build, and serve it. A blueprint toolchain failure must not turn an
   otherwise successful Roadmap run into a failure; report it as an optional
   visualization limitation.

9. Report: tier-1 and tier-2 counts, Mathlib-status breakdown
   (in-mathlib / partial / missing), the dashboard URL, any unresolved gaps,
   and the next step: run Orchestrate.

## Resume semantics

Derive readiness from `graph.json`; do not rely on chat history. A crash loses
only in-flight subagent work. Completed merges, queue entries, verdicts, and
content files remain authoritative.
