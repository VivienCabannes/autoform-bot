---
name: orchestrate
description: Work through an Autoform Markdown blueprint with native agents and Lean tools.
---

Treat the links in `blueprint/nodes/**/*.md` as the source of truth, work ready nodes with native subagents plus the Lean LSP and REPL servers, and record the resulting status and Lean declaration in each node's frontmatter. Search local Mathlib or community prior art with host-native tools when useful, then verify every candidate with Lean before reporting completion.

For a concrete source-to-node handoff, consult the concise [Cabannes thesis walkthrough](references/thesis-worked-node.md). It illustrates dependency-based scheduling, not a theorem or declaration to copy.
