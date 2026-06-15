---
name: plan-json-schema
description: Full schema reference for the v2 tiered plan — graph.json (structure) plus per-node informal_content/<id>.md (prose)
---

# Plan Data Model Reference (v2)

A formalization plan is a **tiered dependency graph**: coarse concept clusters at tier 1, fine definitions/statements at tier 2, and (in the future) Lean statements at tier 3. The model is deliberately generic across tiers — nothing in the schema hard-codes "three"; a node simply names its tier and points at its container one tier up. Everything coarser is *computed*, not stored.

The plan lives in the user's project directory (alongside `lakefile.toml`) and is split across two kinds of file: one global structure file and one prose file per node.

## The two-file encoding

Structure and content are kept apart, because they have different owners, different change patterns, and different readers.

- **`graph.json`** — a single JSON object: top-level metadata plus a map of nodes keyed by `id`. Each node carries only *structural* fields (tier, parent, edges, Mathlib correspondence, bookkeeping). It has a **single writer**: each result is folded in incrementally through one deterministic merge step, which serializes concurrent subagents and keeps the file valid throughout, so a long run stays durable and persisting a node costs nothing as the graph grows. The derived (quotient) edges are a function of the whole graph, recomputed at re-projection and at export rather than on each merge.
- **`informal_content/<id>.md`** — one Markdown file per node, holding that node's mathematical prose: the paraphrased universal-voice statement, and its proof when the node is not in Mathlib. Subagents write these. The filename is a slug derived from the node's `id`, and a node's `content` field records the path. Keeping prose out of `graph.json` keeps the structure file readable and diffable, and maps each node directly onto a blueprint environment at export time.

The two files are linked only by `id`: a node in `graph.json` names its prose file via `content`, and that field is `null` until the prose has been written.

## Top-level structure of `graph.json`

```jsonc
{
  "version": 2,
  "metadata": { ... },
  "nodes": {
    "Markov's inequality": { ... },
    "Sub-Gaussian variables": { ... }
  }
}
```

`nodes` is a **map keyed by `id`**, not an array — lookups by id are the dominant operation (resolving `parent` and `depends_on`, recomputing quotient edges), and keying by id makes the uniqueness invariant structural rather than something to police.

### `version` (integer, required)

Schema version. Currently `2`. Increment on breaking changes.

### `metadata` (object, required)

```json
{
  "created_at": "2026-06-10T14:30:00Z",
  "last_updated": "2026-06-10T15:45:00Z",
  "sources": [
    {"file": "high_dim_stats.pdf", "title": "High-Dimensional Statistics", "format": "pdf"}
  ]
}
```

`created_at` and `last_updated` are ISO 8601 timestamps. `sources` lists the textbooks the plan is built from; each entry has `file` (path relative to the project root), `title`, and `format` (one of `"latex"`, `"markdown"`, `"pdf"`).

## Node (structural) fields

Every node — whatever its tier — has the same shape. The fields below are *structural* and live in `graph.json`; the mathematical prose lives in the linked `informal_content/<id>.md`.

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | Unique identifier — the concept's ordinary English name, written out in full and verbatim (e.g. `Markov's inequality`, `Sub-Gaussian variables`). Apostrophes, spaces, and capitals are fine; the exporter generates a slug-safe label for rendering and escapes the name for display. Book numbering (e.g. "Theorem 1.6") belongs in `source_refs`, never in the id. |
| `tier` | integer | The node's tier: `1` (coarse concept cluster), `2` (fine definition/statement), or `3` (Lean statement, future). |
| `parent` | string or null | The `id` of this node's container exactly one tier up — its cluster, for a tier-2 node. `null` for tier-1 nodes (which have no container), and also permitted *transiently* for a finer node that fits no existing container while a tier is being built — an orphan the re-projection step then assigns or gives a new container. **This is the only authored hierarchy link** (see below). |
| `kind` | string | One of `"definition"`, `"theorem"`, `"proposition"`, `"lemma"`, `"corollary"`, `"example"`. |
| `description` | string | A brief informal summary of the node's mathematical content — a sentence or two, enough for a reviewer to grasp the concept without opening the source. This is the structural one-liner; the full paraphrased statement/proof lives in `informal_content/<id>.md`. |
| `provisional_members` | array of strings | **Tier-1 only, Phase-1 scratch.** The names of the statements a cluster is expected to contain, recorded during coarse extraction to guide Phase-2 splitting. Distinct from the derived `members` (below): this is an authored planning hint, not the live child list. The splitter treats it as a guide, not a contract, and it is ignored once the cluster has been split. |
| `depends_on` | array of strings | The `id`s this node depends on **within its own tier** — the prerequisites needed to define or prove it. Cross-tier dependencies are never written here; they are recovered by the quotient rule. |
| `mathlib_status` | string | One of `"in-mathlib"`, `"partial"`, `"missing"` (see table below). |
| `mathlib_declarations` | array of strings | Mathlib declaration names corresponding to this node, e.g. `["ProbabilityTheory.measure_ge_le_exp_mul_mgf"]`. Empty or absent when the node is `missing`. |
| `mathlib_file` | string | Path to the primary Mathlib source file, e.g. `"Mathlib/Probability/Moments/Basic.lean"`. Absent when `missing`. |
| `mathlib_notes` | string | Free text on the Mathlib correspondence: generality or naming differences, how to import it, why the match is partial. |
| `source_refs` | array | **Internal provenance only — never rendered** in the published content. Records where the concept appears in the sources, for faithfulness-checking. Each entry has `file` and `location` (free text: chapter, section, page). |
| `content` | string or null | Path to this node's prose file, e.g. `"informal_content/markovs-inequality.md"`. `null` until the prose has been written. |

### What is *not* a field

A node never stores its live children, and never stores cross-tier edges. Specifically, `members` (the actual list of a node's children) and the coarser-tier edges are **derived**, not recorded — they are recomputed on demand from the `parent` pointers and the fine `depends_on` edges. Storing them would invite drift; leaving them out makes the structure self-consistent by construction. (The tier-1 `provisional_members` field above is a different thing: an authored Phase-1 *hint* about what a cluster will contain, not the live child list — it is never used to derive anything.)

### `mathlib_status` values

| Value | Meaning |
|-------|---------|
| `"in-mathlib"` | The concept exists in Mathlib, possibly under a different name or in greater generality. These are the **green roots** that ground the graph. |
| `"partial"` | Key components exist but the exact statement needs assembly, or the match is uncertain. A whole tier-1 cluster is usually `partial`. |
| `"missing"` | The concept is not in Mathlib. A `missing` node needs prose (statement **and** proof) and must trace down to green roots. |

## The hierarchy invariant (the spine)

The tiers are kept in sync by a single rule: **`parent` is the only authored hierarchy link, and everything coarser is a projection of the finer tier** — except a coarse node's own descriptive metadata, which is curated to match.

- **Members (children) are derived.** A node's children are exactly the nodes whose `parent` points back at it. The list is never stored; it is read off the `parent` pointers when needed.
- **The coarser node set is derived.** The tier-1 nodes are exactly the distinct `parent` values of the tier-2 nodes: an empty cluster is pruned, and a finer node pointing at a new parent materialises a new coarse node.
- **Coarser-tier edges are the quotient of the finer tier's edges.** A tier-1 cluster A has an edge to a tier-1 cluster B (with A ≠ B) **iff some tier-2 node inside A depends on some tier-2 node inside B**. The same rule relates tier-2 to tier-3 once tier 3 exists.
- **A coarse node's metadata is curated, not derived.** Its name, description, and `mathlib_status` rollup are judgments about the grouping, re-curated to match whatever membership the finer tier dictates.

Because the coarse graph is a function of the fine graph, the two can never drift out of sync. This is what makes parallel construction safe: subagents author only fine `parent`/`depends_on` edges, and the orchestrator — the single serialization point — re-projects the coarse tier from the whole graph.

The finest built tier is authoritative. During Phase 1 there is no tier 2 yet, so tier 1 is authored directly — but only as a **scaffold**. Phase 2 builds the authoritative tier-2 graph (free to add, drop, and re-parent nodes), after which tier 1 is **re-projected** from it: node set and edges recomputed mechanically, metadata re-curated. When tier 3 arrives it is built from tier 2 the same way, and tiers 2 and 1 re-projected from it.

## Invariants

1. **Ids are unique.** Since `nodes` is keyed by `id`, the map enforces this; no two nodes share an id.
2. **Every `parent` resolves.** A non-null `parent` names an existing node exactly one tier up (a tier-2 node's parent is a tier-1 node). Tier-1 nodes have `parent: null`. Equivalently: every tier-2 node has a tier-1 parent.
3. **Every `depends_on` target resolves and stays within the tier.** Each id in a node's `depends_on` names an existing node of the *same* tier.
4. **The dependency graph is a DAG within each tier.** No cycles among same-tier `depends_on` edges. (The derived coarse graph is then also acyclic.)
5. **Every `missing` node reaches a green root.** Following `depends_on` from any `missing` node leads, within finitely many steps, to an `in-mathlib` node. No `missing` node is left unsupported; roots of the graph (nodes with empty `depends_on`) are `in-mathlib`.
6. **`content` matches reality.** `content` is `null` exactly when no prose file exists for the node; otherwise it points at the existing `informal_content/<id>.md`.
7. **`source_refs` is never rendered.** It is internal bookkeeping only and must not leak into the published prose.

## Worked example

A small two-tier graph: two tier-1 clusters (`Concentration inequalities` depending on `Moment methods`) and three tier-2 nodes distributed across them.

### `graph.json`

```jsonc
{
  "version": 2,
  "metadata": {
    "created_at": "2026-06-10T14:30:00Z",
    "last_updated": "2026-06-10T15:45:00Z",
    "sources": [
      {"file": "high_dim_stats.pdf", "title": "High-Dimensional Statistics", "format": "pdf"}
    ]
  },
  "nodes": {
    "Moment methods": {
      "id": "Moment methods",
      "tier": 1,
      "parent": null,
      "kind": "definition",
      "depends_on": [],
      "mathlib_status": "partial",
      "mathlib_declarations": [],
      "mathlib_file": "Mathlib/Probability/Moments/Basic.lean",
      "mathlib_notes": "Moment generating functions present; cluster assembled from several files.",
      "source_refs": [{"file": "high_dim_stats.pdf", "location": "Ch 1, §1.2"}],
      "content": null
    },
    "Concentration inequalities": {
      "id": "Concentration inequalities",
      "tier": 1,
      "parent": null,
      "kind": "theorem",
      "depends_on": ["Moment methods"],
      "mathlib_status": "partial",
      "mathlib_declarations": [],
      "mathlib_file": "Mathlib/Probability/Moments/SubGaussian.lean",
      "mathlib_notes": "Markov/Chernoff present; sub-Gaussian theory partially formalized.",
      "source_refs": [{"file": "high_dim_stats.pdf", "location": "Ch 1, §1.3"}],
      "content": null
    },

    "Moment generating function": {
      "id": "Moment generating function",
      "tier": 2,
      "parent": "Moment methods",
      "kind": "definition",
      "depends_on": [],
      "mathlib_status": "in-mathlib",
      "mathlib_declarations": ["ProbabilityTheory.mgf"],
      "mathlib_file": "Mathlib/Probability/Moments/Basic.lean",
      "mathlib_notes": "Defined as the expectation of exp(t·X).",
      "source_refs": [{"file": "high_dim_stats.pdf", "location": "Ch 1, Def 1.2"}],
      "content": "informal_content/moment-generating-function.md"
    },
    "Markov's inequality": {
      "id": "Markov's inequality",
      "tier": 2,
      "parent": "Concentration inequalities",
      "kind": "theorem",
      "depends_on": [],
      "mathlib_status": "in-mathlib",
      "mathlib_declarations": ["MeasureTheory.mul_meas_ge_le_lintegral"],
      "mathlib_file": "Mathlib/MeasureTheory/Integral/Lebesgue.lean",
      "mathlib_notes": "Markov's inequality for nonnegative measurable functions.",
      "source_refs": [{"file": "high_dim_stats.pdf", "location": "Ch 1, Thm 1.5"}],
      "content": "informal_content/markovs-inequality.md"
    },
    "Chernoff bound": {
      "id": "Chernoff bound",
      "tier": 2,
      "parent": "Concentration inequalities",
      "kind": "theorem",
      "depends_on": ["Markov's inequality", "Moment generating function"],
      "mathlib_status": "partial",
      "mathlib_declarations": ["ProbabilityTheory.measure_ge_le_exp_mul_mgf"],
      "mathlib_file": "Mathlib/Probability/Moments/Basic.lean",
      "mathlib_notes": "Generic exponential-Markov bound present; the optimized form is assembled.",
      "source_refs": [{"file": "high_dim_stats.pdf", "location": "Ch 1, Thm 1.6"}],
      "content": "informal_content/chernoff-bound.md"
    }
  }
}
```

Notice what is **absent**: neither tier-1 cluster lists its members, and the tier-1 edge `Concentration inequalities → Moment methods` is shown here only as a Phase-1 authored scaffold edge. Once tier 2 exists, that edge is *derived*: the tier-2 node `Chernoff bound` (in `Concentration inequalities`) depends on `Moment generating function` (in `Moment methods`), so the quotient yields exactly that coarse edge. The `Markov's inequality → Chernoff bound` part of the graph is internal to the `Concentration inequalities` cluster and contributes no coarse edge.

### `informal_content/chernoff-bound.md`

Because `Chernoff bound` is `partial` rather than fully `in-mathlib`, its prose carries both a statement and a proof, reorganized around *our* prerequisite nodes (not the book's lemma numbers), in one consistent Mathlib-aligned notation and an uncited universal voice:

```markdown
# Chernoff bound

Let $X$ be a real random variable whose moment generating function
$M_X(t) = \mathbb{E}[e^{tX}]$ is finite for $t$ in a neighborhood of $0$.
Then for every $a \in \mathbb{R}$,
$$ \mathbb{P}(X \ge a) \le \inf_{t > 0} e^{-ta} M_X(t). $$

## Proof

Fix $t > 0$. The event $\{X \ge a\}$ coincides with $\{e^{tX} \ge e^{ta}\}$,
since $x \mapsto e^{tx}$ is strictly increasing. Applying Markov's inequality
to the nonnegative random variable $e^{tX}$ gives
$$ \mathbb{P}(X \ge a) = \mathbb{P}(e^{tX} \ge e^{ta})
   \le e^{-ta}\,\mathbb{E}[e^{tX}] = e^{-ta} M_X(t). $$
As this holds for every $t > 0$, taking the infimum over $t > 0$ yields the claim.
```

An `in-mathlib` node such as `Markov's inequality` would instead carry only a statement and a pointer to its Mathlib declaration — no paraphrased proof, since Mathlib already has one.
