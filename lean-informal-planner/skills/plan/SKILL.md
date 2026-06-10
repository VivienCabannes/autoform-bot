---
name: plan
description: >
  This skill should be used when the user asks to "plan a formalization",
  "build a dependency graph", "map concepts to Mathlib", "analyze a textbook
  for formalization", "create a formalization plan", "chart mathematical concepts",
  or wants to plan Lean 4 formalization work from textbook sources.
version: 0.1.0
---

# Formalization Planning

Build a dependency graph of mathematical concepts from textbooks, map each concept to Mathlib, identify gaps, and produce an interactive visualization. The result is a `plan.json` file and an interactive HTML graph.

## Guiding Principle

**Textbooks are the source of truth.** Read and reference the provided material rather than relying on training knowledge. When a concept's prerequisites are unclear from the available material, ask the user for additional reference books rather than guessing. Be explicit about uncertainty.

## Workflow

### Phase 1: Extract Target Concepts

Read the provided textbook(s) — supports LaTeX (`.tex`), Markdown (`.md`), and PDF (`.pdf`, using visual PDF reading at ~20 pages per call). For PDFs, read systematically through the document in chunks.

For the user-specified scope (chapters, sections, or "all content"), identify all mathematical statements:
- Definitions, theorems, propositions, lemmas, corollaries, examples
- Assign each an ID with standard prefix (`def-`, `thm-`, `prop-`, `lem-`, `cor-`, `ex-`) and chapter-section numbering
- Capture: name, kind, brief informal description, source location
- Record dependencies visible from the text (explicit cross-references)

Write the initial concept list to `plan.json` using the schema in `references/plan-json-schema.md`.

### Phase 2: Build Full Graph (Iterative)

This phase is a loop that expands the graph downward until all root nodes are in Mathlib.

**Wave 1:**
- Lay out target concepts and their direct dependencies from the textbook
- Fan out Mathlib checks on all leaf nodes — spawn one `mathlib-checker` agent per concept (these run in parallel when using workflow orchestration)
- Read results and update `mathlib_status` for each concept

**Wave 2+:**
- For leaves NOT in Mathlib, identify deeper prerequisites:
  - If the textbook covers them → extract from the book and add as non-target nodes (`is_target: false`)
  - If not → **ask the user for additional reference material**, specifying what topic needs coverage
- Fan out Mathlib checks on newly added leaf nodes
- Repeat until all leaf nodes are in Mathlib, or flag that more reference material is needed

Non-target intermediate concepts are first-class graph nodes — they're essential for a complete plan.

**Important:** During graph construction, whenever mathematical knowledge is needed to determine prerequisites, look it up in the provided textbooks first. Only fall back to training knowledge for well-established facts (e.g., "a group homomorphism requires groups").

### Phase 3: Review (Parallel by Lens)

Spawn reviewer agents in parallel, each examining the full graph:

- **`graph-reviewer`**: Checks edge correctness (is each edge justified?), finds missing edges, identifies redundant/mergeable nodes. References the textbook.
- **`gap-finder`**: Identifies large conceptual jumps that need intermediate nodes. Flags when reference material is needed.

Each reviewer returns structured findings (edges to add/remove, nodes to add/merge).

### Phase 4: Reconcile

Process reviewer findings:
- **High-confidence corrections** (clear justification, no conflicts): apply automatically and note the change
- **Uncertain or conflicting suggestions**: surface to the user with the reviewers' reasoning, ask for a decision
- Run Mathlib checks on any newly added nodes

Loop Phases 3-4 until reviewers find nothing new (max 3 rounds).

### Phase 5: Holistic Review

Spawn the `holistic-reviewer` agent on the complete graph. This agent checks:
- Overall coherence and whether the graph tells a sensible mathematical story
- Consistent granularity across different areas
- Whether Mathlib roots are reasonable starting points
- Coverage gaps the specialized reviewers missed
- Suggested formalization order

If the holistic reviewer finds significant issues, run one more targeted pass of Phases 3-4.

### Phase 6: Generate Visualization

Run the visualization script to produce `plan_graph.html`:

```bash
python ${CLAUDE_PLUGIN_ROOT}/scripts/generate_graph.py <path-to-plan.json>
```

Open the resulting HTML file in the user's browser. The visualization shows:
- Color-coded DAG (green = in Mathlib, amber = partial, red = missing, gray = unchecked)
- Clickable nodes with detail panels
- Filter by status
- Summary statistics

## Agent Usage

When running with workflow orchestration (ultracode), use the following patterns:

- **Mathlib checking**: Fan out one `mathlib-checker` agent per concept using `parallel()`. These are independent and embarrassingly parallel. Use sonnet model for cost efficiency.
- **Review**: Run `graph-reviewer` and `gap-finder` in `parallel()`. They examine the full graph from different angles.
- **Holistic review**: Run `holistic-reviewer` as a single agent after reconciliation.

Without workflow orchestration, perform each step sequentially — the workflow still works, just slower.

## Self-Critique Protocol

All agents include self-critique instructions. When processing agent results, check for `## ⚠️ Issue` sections at the top of their output. If found:
- Surface the issue to the user immediately
- Include the agent's suggested improvements
- Ask whether to proceed, adjust, or provide additional reference material

## Additional Resources

### Reference Files

For the full `plan.json` schema with all fields, types, invariants, and examples:
- **`references/plan-json-schema.md`** — Complete plan.json schema reference
