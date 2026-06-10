---
name: plan-json-schema
description: Full schema reference for plan.json, the central data format for formalization plans
---

# plan.json Schema Reference

The `plan.json` file is the central data store for a formalization plan. It lives in the user's project directory (alongside `lakefile.toml`) and is read/written directly by Claude.

## Top-Level Structure

```json
{
  "version": 1,
  "metadata": { ... },
  "concepts": [ ... ],
  "summary": { ... }
}
```

## Fields

### `version` (integer, required)

Schema version. Currently `1`. Increment on breaking changes.

### `metadata` (object, required)

```json
{
  "created_at": "2026-06-10T14:30:00Z",
  "last_updated": "2026-06-10T15:45:00Z",
  "sources": [
    {
      "file": "topology.tex",
      "title": "Introduction to Algebraic Topology",
      "format": "latex"
    },
    {
      "file": "analysis.pdf",
      "title": "Real Analysis",
      "format": "pdf"
    }
  ]
}
```

- `created_at`: ISO 8601 timestamp of initial plan creation.
- `last_updated`: ISO 8601 timestamp of last modification.
- `sources`: Array of textbooks used. Each has:
  - `file`: Path to the source file (relative to project root).
  - `title`: Human-readable title.
  - `format`: One of `"latex"`, `"markdown"`, `"pdf"`.

### `concepts` (array, required)

Array of concept objects. Each concept represents a mathematical definition, theorem, lemma, proposition, or corollary.

```json
{
  "id": "thm-2.3",
  "name": "Compact subsets of Hausdorff spaces are closed",
  "kind": "theorem",
  "description": "If K is a compact subset of a Hausdorff space X, then K is closed in X. The proof uses the Hausdorff separation axiom to construct, for each point outside K, a neighborhood disjoint from K.",
  "source_refs": [
    {"file": "topology.tex", "location": "Chapter 2, Theorem 2.3, page 42"}
  ],
  "is_target": true,
  "mathlib_status": "in-mathlib",
  "mathlib_declarations": ["IsCompact.isClosed"],
  "mathlib_file": "Mathlib/Topology/Separation/Basic.lean",
  "mathlib_notes": "Direct match. Requires [T2Space X].",
  "depends_on": ["def-1.1", "def-1.5", "def-2.1"]
}
```

#### Concept Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier. Use prefix: `def-`, `thm-`, `prop-`, `lem-`, `cor-`, `ex-` followed by chapter-section numbering. |
| `name` | string | yes | Short human-readable name (e.g., "Central Limit Theorem", "Singular Homology"). |
| `kind` | string | yes | One of: `"definition"`, `"theorem"`, `"proposition"`, `"lemma"`, `"corollary"`, `"example"`. |
| `description` | string | yes | Brief informal description of the mathematical content. Should capture what the statement says and key proof ideas, enough for a mathematician to understand without opening the textbook. A few sentences at most. |
| `source_refs` | array | yes | Where this concept appears in the source material. Each entry has `file` (source filename) and `location` (free text: chapter, section, page). |
| `is_target` | boolean | yes | `true` if this concept was explicitly requested by the user as a formalization target. `false` for intermediate concepts added to complete the graph. |
| `mathlib_status` | string | yes | One of: `"in-mathlib"`, `"partial"`, `"missing"`, `"unchecked"`. |
| `mathlib_declarations` | array of strings | no | Mathlib declaration names that correspond to this concept (e.g., `["IsCompact.isClosed"]`). Empty or absent if not in Mathlib. |
| `mathlib_file` | string | no | Path to the primary Mathlib source file (e.g., `"Mathlib/Topology/Separation/Basic.lean"`). |
| `mathlib_notes` | string | no | Free text explaining the Mathlib correspondence: generality differences, naming differences, how to import it. |
| `depends_on` | array of strings | yes | IDs of concepts that this concept depends on (i.e., concepts needed to define or prove this one). |

#### `mathlib_status` Values

| Value | Meaning |
|-------|---------|
| `"in-mathlib"` | The concept exists in Mathlib, possibly under a different name or in greater generality. |
| `"partial"` | Key components exist but the exact statement needs assembly, or the agent is uncertain. |
| `"missing"` | The concept is not in Mathlib. |
| `"unchecked"` | Mathlib status has not yet been determined. |

### `summary` (object, required)

Aggregate statistics, updated whenever the plan changes:

```json
{
  "total": 47,
  "in_mathlib": 31,
  "partial": 8,
  "missing": 6,
  "unchecked": 2
}
```

## Invariants

1. Every `id` in `depends_on` arrays must reference an existing concept in the `concepts` array.
2. The dependency graph must be a DAG (no cycles).
3. All root nodes (concepts with empty `depends_on`) should have `mathlib_status: "in-mathlib"` in a completed plan.
4. All target concepts (`is_target: true`) must be present.
5. `summary` counts must match the actual concept statuses.

## Example: Minimal Plan

```json
{
  "version": 1,
  "metadata": {
    "created_at": "2026-06-10T14:30:00Z",
    "last_updated": "2026-06-10T14:30:00Z",
    "sources": [
      {"file": "intro_topology.md", "title": "Introduction to Topology", "format": "markdown"}
    ]
  },
  "concepts": [
    {
      "id": "def-1.1",
      "name": "Topological Space",
      "kind": "definition",
      "description": "A set X together with a collection of subsets (the topology) closed under finite intersections and arbitrary unions, containing both X and the empty set.",
      "source_refs": [{"file": "intro_topology.md", "location": "Chapter 1, Definition 1.1"}],
      "is_target": true,
      "mathlib_status": "in-mathlib",
      "mathlib_declarations": ["TopologicalSpace"],
      "mathlib_file": "Mathlib/Topology/Basic.lean",
      "mathlib_notes": "Defined as a typeclass with IsOpen predicate.",
      "depends_on": []
    },
    {
      "id": "def-1.2",
      "name": "Continuous Function",
      "kind": "definition",
      "description": "A function f : X → Y between topological spaces is continuous if the preimage of every open set is open.",
      "source_refs": [{"file": "intro_topology.md", "location": "Chapter 1, Definition 1.2"}],
      "is_target": true,
      "mathlib_status": "in-mathlib",
      "mathlib_declarations": ["Continuous"],
      "mathlib_file": "Mathlib/Topology/Basic.lean",
      "mathlib_notes": "Defined via IsOpen preimage condition. The Continuous predicate.",
      "depends_on": ["def-1.1"]
    }
  ],
  "summary": {
    "total": 2,
    "in_mathlib": 2,
    "partial": 0,
    "missing": 0,
    "unchecked": 0
  }
}
```
