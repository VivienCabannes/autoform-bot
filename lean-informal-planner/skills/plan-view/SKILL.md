---
name: plan-view
description: >
  This skill should be used when the user asks to "view the plan",
  "show the graph", "open the visualization", "regenerate the graph",
  "refresh the plan view", or wants to see the interactive dependency
  graph for a formalization plan.
version: 0.1.0
---

# Plan Visualization

Regenerate and open the interactive HTML visualization for a formalization plan.

## Usage

To generate or refresh the visualization from a `plan.json` file:

```bash
python ${CLAUDE_PLUGIN_ROOT}/scripts/generate_graph.py <path-to-plan.json>
```

This produces `plan_graph.html` in the same directory as `plan.json`.

Open the HTML file in the user's default browser. On macOS:

```bash
open <path-to-plan_graph.html>
```

## When to Use

- After creating or updating a formalization plan
- When the user asks to see the current state of the graph
- After manual edits to `plan.json`
- To share the plan with collaborators (the HTML file is self-contained)

## What the Visualization Shows

- **Dagre layout**: Top-to-bottom directed acyclic graph
- **Color-coded nodes**: 🟢 green (in Mathlib), 🟡 amber (partial), 🔴 red (missing), ⚪ gray (unchecked)
- **Node shapes**: Rectangles for target concepts, ellipses for intermediates
- **Click a node**: Side panel with description, source references, Mathlib status, declarations, dependencies, and dependents
- **Filters**: Toggle visibility by Mathlib status
- **Summary bar**: Total counts by status
