---
name: roadmap
description: >-
  Build, inspect, refine, or visualize the mathematical roadmap and theorem
  dependency DAG in an existing Autoform Markdown blueprint. Use for discovering
  prior work, choosing or drafting mathematical sources, confirming scope,
  writing roadmap and coverage notes, decomposing mathematics into Markdown
  article pages under blueprint/roadmap/, or
  checking roadmap completeness; do not install repository infrastructure or
  prove Lean declarations.
---

# Build an Autoform roadmap

Turn an agreed mathematical specification into a human-editable roadmap and a
DAG of pull-request-sized formalization units. Keep Markdown as the sole source
of truth. Use `internal/runbooks/planning.md` for the preserved detailed planning
workflow when this concise skill needs deeper operational guidance.

## Discover prior work and sources

Before fixing the architecture, search the pinned Mathlib checkout for existing
primitives and gaps. When network access is available, also make targeted,
read-only searches of relevant GitHub pull requests and issues, Zulip topics,
and authoritative mathematical literature. Report overlapping work, active
contributors, design rationale, and candidate references; never contact people
or post externally without explicit user approval.

For Zulip discovery or project coordination, follow Setup's
[shared opt-in Zulip workflow](../setup/references/zulip.md).

Distinguish implementation prior art from mathematical sources. Let the user
choose whether to adopt a reference, ask the agent to locate one, or develop a
project-authored specification collaboratively. For the last option, brainstorm
the representation and downstream API first, then record explicit definitions,
assumptions, intended equivalences, and unresolved choices under
`blueprint/sources/`; label this material as project-authored rather than
implying external provenance. Do not expand a fine DAG until its statements are
grounded in an adopted reference or this agreed specification.

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
   for every definition or theorem used in the plan. Work from the source text;
   for large sources, use targeted lookups for a named result or definition and
   record the exact passage rather than relying on memory. Surface prerequisites
   not covered by the confirmed sources instead of inventing them. These notes
   are vault material, not chapters: the site does not publish them, and a
   statement's `## Sources` list is rewritten to the file in the repository, so
   write them for a reader with the repository open.
2. Write the high-level direction and milestones under `blueprint/roadmap/`.
   Group milestones by coherent mathematical significance, not by source
   section size.
   A chapter page needs no frontmatter at all: its H1 is the title and its
   position in the tree is its identity. Write it as
   `roadmap/<chapter>/README.md`, never as a Markdown file beside the
   directory: containment is inferred from nested READMEs, so a sibling
   chapter page silently leaves every article in that directory hanging off
   the root, and `autoform audit` reports `missing-chapter-article`. Treat `blueprint/README.md` and the roadmap pages it
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
5. After approval, create one file per pull-request-sized unit beside its
   milestone under `blueprint/roadmap/**/*.md`. A node may contain several
   supporting definitions or statements when they should land and be reviewed
   together, but it must identify one unique main result that determines when
   the node is complete. Nothing authored in an article predicts how much Lean
   it will take, so "pull-request-sized" cannot be checked up front;
   `autoform audit --lean-root` reports `node-too-large` afterwards, against
   the project's own median. Read that finding as a decomposition bug in this
   roadmap rather than a Lean problem, and split the node. Its path relative to `roadmap/`, without `.md`, is its
   stable ID. Give it exactly one H1, a `declaration` naming the kind of Lean
   artifact, a source-grounded statement or proof sketch, and a
   `## Depends on` section.
6. Put only genuine prerequisite links under `## Depends on`; those relative
   Markdown links are the machine-read DAG edges. Use `## Proof depends on` for
   a prerequisite the proof needs but the statement does not. Keep roadmap,
   coverage, and source links under other headings.
7. Search the pinned Mathlib checkout before planning new work. Set
   `mathlib: true` only for an exact verified upstream result; record partial or
   uncertain candidates as notes, never as formalization status.
8. Reconcile every page whose claims this work has just invalidated. That means
   the coarse milestone pages and the coverage contract, and also the two
   landing pages Setup wrote before any scope existed: `blueprint/README.md`
   and the repository `README.md`. Setup states there that no chapters exist
   and nothing is planned. The moment a chapter exists that is false, and it is
   the first thing a visitor to the published site reads. Newly discovered
   prerequisites, moved units, and deferred scope must not leave any of these
   stale either.

Assert only what is checked: `statement: formalized`, `proof: formalized`,
`mathlib: true`, `not_ready: true`, and the compiled name in `lean`. Ready,
blocked, and fully-proved are derived from the DAG — never hand-write them, and
never start proof workers merely to advance a state. The
[blueprint format reference](../../autoform_cli/README.md) has the full table.

## Validate and report

Validate `<PROJECT>/blueprint` and refresh its Mermaid graph before handing
off. Rendering and the strict site build belong to Setup and Human Review; here
only the first two steps of the publication sequence in the
[CLI reference](../../autoform_cli/README.md#commands) apply.

Fix missing targets, escaping links, self-dependencies, cycles, and unresolved
`lean:` names before handoff.

Commit the vault once `autoform check` passes. CI renders and publishes from
the repository, not from your working tree, so roadmap work left uncommitted
leaves the site advertising a project with no chapters. Pushing is
outward-facing: report the command and let the user run it, unless they have
already asked you to push.

Report roadmap and coverage status, node and edge
counts, the derived state summary, unresolved source questions, and the
vault/graph paths. Hand nodes that are ready to state or prove to Orchestrate;
hand the draft to Agent Review for mathematical-plan judgment or Human Review
for visual inspection; hand CI, Pages, Lean-project, or vault infrastructure
changes back to Setup.
