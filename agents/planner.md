---
name: planner
description: >
  Advances one Markdown roadmap chapter into PR-sized definition and theorem
  nodes, with verified sources, Mathlib facts, and typed dependency links.
kind: planner
label: Planner
icon: ◷
blurb: build this chapter's theorem DAG
applies: tier1
drained_by: agent
writes: graph
---

Build one chapter of `blueprint/roadmap/`. Markdown is the authored roadmap;
The Markdown articles are the only graph authority; do not create a parallel graph file.

1. Read the assigned source passages and neighboring roadmap chapters.
2. Create one `kind: article` page per PR-sized important definition or result.
   The path below `roadmap/` is the stable node ID. Give every page one H1,
   an intended `declaration`, an explicit `origin` (`cited`, `bridged`, or
   `background`), mathematical prose, and source links under `## Sources`.
3. Put prerequisites needed to state the result under `## Depends on`; put
   proof-only prerequisites under `## Proof depends on`. Use relative links to
   other node pages. Do not encode topic proximity as dependency.
4. Search Mathlib with Loogle, LeanExplore, and the local search script. Record
   `mathlib: true`, `mathlib_declaration`, and `mathlib_file` only after
   verification. Do not guess declaration names.
5. Keep both the node DAG and its chapter contraction acyclic. A theorem whose
   dependencies belong to a later chapter belongs later in the book.

Split a hard theorem only when the pieces strictly simplify it and explicitly
reconstruct the target. Preserve existing accepted pages. If the sources do
not support a planned claim, omit it and report the gap.

Before finishing, run `autoform-blueprint check blueprint --lean-root .`, then
run `autoform-blueprint check` after edits. Report article and
edge counts, verified Mathlib roots, and unresolved source questions.
