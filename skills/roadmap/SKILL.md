---
name: roadmap
description: >-
  Build, inspect, refine, or visualize the mathematical roadmap and theorem
  dependency DAG in an existing Autoform Markdown blueprint. Use for confirming
  source scope, writing roadmap and coverage notes, decomposing mathematics
  into kind: node Markdown pages under blueprint/roadmap/, setting planning
  statuses, or checking roadmap
  completeness; do not install repository infrastructure or prove Lean
  declarations.
---

# Build an Autoform roadmap

Turn confirmed mathematical sources into a human-editable roadmap and a
theorem-sized DAG. Keep Markdown as the sole source of truth.

## Establish the planning boundary

Inspect the Lean repository and existing `blueprint/` before writing. Require
the vault, ignore rules, and site configuration to exist; if they do not, hand
the task to Setup rather than creating repository infrastructure here.

State the exact source files, requested chapters or sections, current roadmap
state, and files that may change. Do not infer missing scope or silently reset
existing notes. Preserve accepted material and ask before replacing it.

Read the concise [Cabannes thesis roadmap](references/cabannes-thesis-roadmap.md)
when a concrete source-to-DAG pattern is useful. Adapt its method, never its
mathematics.

## Build from coarse to fine

1. Record source notes under `blueprint/sources/`, including stable locations
   for every definition or theorem used in the plan.
2. Write the high-level direction and milestones under `blueprint/roadmap/`.
   Begin each planning page with simple YAML scalar properties such as
   `kind: roadmap` and `status: active` so Obsidian and people can see its
   state immediately. Treat `blueprint/README.md` and the roadmap pages it
   links as an ordered mathematical book: link meaningful chapter pages in
   their intended reading order. The renderer derives bottom-of-page previous
   and next chapter links from this Markdown structure, so do not maintain a
   second navigation manifest.
3. Define project-specific coverage targets and completion rules under
   `blueprint/coverage/`. Distinguish material that is merely mapped from
   material decomposed into nodes; never report whole-source completion from a
   partial theorem slice.
4. Present this coarse roadmap and coverage contract for user approval before
   expanding it into a fine DAG.
5. After approval, create one file per formalization-sized definition or
   statement beside its milestone under `blueprint/roadmap/**/*.md`. Set
   `kind: node`; its path relative to `roadmap/`, without `.md`, is its stable
   ID. Give it exactly one H1, a `declaration` naming the intended Lean
   artifact, a source-grounded statement or proof sketch, and a
   `## Depends on` section. Never overload `kind` with the declaration.
6. Put only genuine prerequisite links under `## Depends on`; those relative
   Markdown links are the machine-read DAG edges. Use `## Proof depends on` for
   a prerequisite the proof needs but the statement does not. Keep roadmap,
   coverage, and source links under other headings.

Assert only what is checked: `statement: formalized`, `proof: formalized`,
`mathlib: true`, `not_ready: true`, and the compiled name in `lean`. Ready,
blocked, and fully-proved are derived from the DAG — never hand-write them, and
never start proof workers merely to advance a state. The
[blueprint format reference](../../autoform_cli/README.md) has the full table.

## Validate and report

`<AUTOFORM_PLUGIN_ROOT>` is the AutoformBot checkout this skill was loaded
from; substitute its absolute path and run:

```bash
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check "<PROJECT>/blueprint" \
  --lean-root "<PROJECT>"
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform-visualize "<PROJECT>/blueprint"
```

Fix missing targets, escaping links, self-dependencies, cycles, and unresolved
`lean:` names before handoff. Report roadmap and coverage status, node and edge
counts, the derived state summary, unresolved source questions, and the
vault/graph paths. Hand nodes that are ready to state or prove to Orchestrate;
hand CI, Pages, Lean-project, or vault infrastructure changes back to Setup.
