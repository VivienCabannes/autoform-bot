# Lean Informal Planner — v2 Design

## Context

v1 built a single-tier dependency graph of textbook concepts mapped to Mathlib, rendered as a self-contained Cytoscape HTML. It works, but the run on Rigollet's *High-Dimensional Statistics* exposed two limits: nodes were one-statement-each (too granular for a high-level map), and the graph carried no actual mathematical content.

v2 makes the graph **tiered** and gives it **content**, and replaces the bespoke viewer with a **fork-free adaptation of `leanblueprint`**:

- **Tier 1** — coarse concept clusters (a high-level concept + its associated results), the quick scoping map.
- **Tier 2** — fine nodes (one definition/statement; hard theorems split into a few), each carrying a full *paraphrased, universal-voice* statement (and proof when not in Mathlib).
- **Tier 3** — Lean statements (future); planned for, not built.

Tiers are strictly hierarchical, and that hierarchy is the spine of the whole design (below).

This document is the authoritative v2 plan. It supersedes the v1 plan for Phases 1–2 and the visualization; the MCP Mathlib server, the SessionStart hook, and the `mathlib-checker`/`graph-reviewer`/`gap-finder`/`holistic-reviewer` agents carry over (revised).

---

## 1. The hierarchy invariant (the spine)

Every node has a `tier` and a single `parent` — its container one tier up (tier-1 nodes have `parent: null`). **`parent` is the only authored hierarchy link.** From it, everything about a coarser tier follows from the finer one:

- `members` (a node's children) — the finer nodes whose `parent` points back at it.
- The coarser **node set** — exactly the distinct `parent` values of the finer nodes: empty clusters are pruned, and a finer node pointing at a new parent materialises a new coarser node.
- Coarser-tier **edges** — the **quotient** of the finer tier's edges: tier-1 has an edge A→B iff some tier-2 node in A depends on some tier-2 node in B (A≠B). The same rule relates tier-2 to tier-3.

The one thing *not* mechanically derived is a coarser node's own **descriptive metadata** (its name, description, `mathlib_status` rollup) — that is *curated* to match the membership the finer tier dictates.

### Authority: the finest built tier wins

The finest tier that has been built is the **source of truth**; every coarser tier is a *projection* of it. Building a new finest tier follows one pattern, reused at every level:

1. Take the current finest tier as a **scaffold** — a guide, not a cage.
2. Build the new finer tier with full freedom to add, delete, re-parent, and re-connect nodes.
3. **Re-project** every coarser tier from it: recompute the node set and edges mechanically, then re-curate the coarse metadata to match.

So Phase 1 authors tier-1 directly (no finer tier exists yet). Phase 2 then treats that tier-1 as a scaffold and builds the authoritative tier-2, after which tier-1 is re-projected from it — the original Phase-1 tier-1 was only ever a starting guide. When tier-3 arrives it will be built from tier-2 the same way, and tier-2 *and* tier-1 re-projected from it. Because each coarser tier is computed from the finer one, the tiers can never drift out of sync.

---

## 2. Encoding

Structure and content are separate:

- **`graph.json`** — one object: top-level metadata + a map of nodes keyed by `id`, each holding only *structural* fields. The main agent owns this file and rewrites it globally (it needs the whole graph in memory to recompute quotient edges).
- **`content/<id>.md`** — one file per node: the universal-voice statement (+ proof). Subagents write these. Keeping prose out of `graph.json` keeps it readable and diffable, and maps directly to a blueprint environment at export.

### Node (structural) fields

```jsonc
{
  "id": "Markov's inequality",      // unique; the concept's ordinary English name, verbatim
  "tier": 2,                         // 1 | 2 | 3
  "parent": "Sub-Gaussian variables",// container one tier up; null for tier 1
  "kind": "theorem",                 // definition|theorem|proposition|lemma|corollary|example
  "depends_on": ["Moment generating function"], // edges WITHIN this tier
  "mathlib_status": "in-mathlib",    // in-mathlib | partial | missing
  "mathlib_declarations": ["ProbabilityTheory.measure_ge_le_exp_mul_mgf"],
  "mathlib_file": "Mathlib/Probability/Moments/Basic.lean",
  "mathlib_notes": "…",
  "source_refs": [{"file": "book.md", "location": "Ch 1, Thm 1.x"}], // INTERNAL provenance only
  "content": "content/markovs-inequality.md"  // path; null until written
}
```

- `id` is the full English name (apostrophes/spaces/capitals fine — the viewer escapes them). `members` and coarse edges are **not** stored (derived).
- `source_refs` is internal bookkeeping for faithfulness-checking — **never rendered** in the published content.

---

## 3. Phase 1 — coarse (tier-1) graph

As in v1 but *grouping, not enumerating*. The extraction instruction (already in `skills/plan/SKILL.md`, to be adjusted):

> Group the material into coarse concept clusters — each a coherent topic (a high-level concept plus its associated results). Granularity tracks **mathematical weight**, not a fixed count: a major theorem can be its own cluster; minor results get grouped. Use the book's section structure as a starting guide, merging/splitting for coherence. Record each cluster's `id` (its English name), a short description, source refs, and a provisional list of the statements it will contain (names only — these become tier-2 nodes in Phase 2).

Then, unchanged from v1 in spirit, at cluster grain: parallel `mathlib-checker` per cluster → ground every cluster down to green Mathlib roots (coarse green Mathlib-area nodes at ~topic-folder granularity, derived from real search hits) → review (`graph-reviewer`, `gap-finder`) → reconcile → end-of-phase holistic review. A cluster is usually `partial`.

Reviewer adjustment: granularity is judged by **significance**, not uniform size.

Phase 1 ends with a user-reviewable coarse graph and its blueprint view. It earns its place: a cheap scoping checkpoint that also hands Phase 2 its starting partition (top-down splitting is far more tractable than bottom-up clustering).

---

## 4. Phase 2 — detailed (tier-2) graph

Tier-1 is a **scaffold, not a cage**. Phase 2 builds the authoritative tier-2 graph, free to add tier-2 nodes a cluster didn't anticipate, drop ones that don't belong, re-parent, and add edges — including to nodes in other clusters or to newly discovered prerequisites. Tier-1 is then re-projected from the result (see "Re-projection" below).

Walk the tier-1 scaffold **top-down in topological waves** (a wave = clusters whose prerequisites are already split), parallelizing within a wave with a barrier between waves. This is a best-effort ordering so that, where possible, a node's prerequisites are written before it (self-contained prose) and shared prerequisites aren't created twice. It is a *guide*, not an invariant: when a splitter discovers a prerequisite that isn't written yet, it adds the node and edge immediately and references it by id; that node gets its content when reached (possibly in a later wave). The main agent tracks nodes still awaiting content.

**Per cluster, a subagent (`splitter`):**
1. Splits the cluster into tier-2 nodes — one per definition/statement; a hard theorem may become a few sub-statements (the split is a claim: the pieces must compose to the original). The cluster's `provisional_members` from Phase 1 are a hint, not a contract — add, drop, or regroup freely.
2. Sets each tier-2 node's `parent` (its cluster) and within-tier `depends_on`. A node that fits no existing cluster gets `parent: null` transiently, or names a proposed new cluster; reconciliation settles it.
3. Writes `content/<id>.md` for each (see content rules).
4. Returns the new/changed/deleted nodes, edges, and content to the main agent, flagging any tier-1 issue the split reveals.

**The main agent** is the serialization point: it merges subagent output into `graph.json`, keeps it consistent (no edges dangling to deleted nodes), recomputes the derived tier-1 edges and membership, and tracks content-pending nodes. It never delegates graph writes — that's what makes wave-parallelism safe.

### Re-projection (end of Phase 2)

Once tier-2 is stable, rebuild tier-1 from it, since tier-2 is now authoritative:
- **Mechanical:** the tier-1 node set = the distinct `parent` values (prune empty clusters, materialise clusters for orphan/new parents); tier-1 edges = the quotient of tier-2 edges.
- **Curated:** re-curate each tier-1 node's metadata to match its new membership — name any new cluster, update descriptions, roll up `mathlib_status`, and merge or split clusters whose composition no longer coheres. This is a judgment pass (run `graph-reviewer` and `holistic-reviewer` over the re-projected tier-1), not a formula — otherwise tier-1 quietly decays into a mechanical rollup.

### Content rules

- **Statement: always**, for every node — so the stitched-together document reads as continuous narrative.
- **Proof: only when not in Mathlib.** An `in-mathlib` node gets a pointer to its Mathlib declaration instead of a paraphrased proof (no rewriting proofs Mathlib already has).
- **Universal, uncited voice** — write it *as Mathlib would*: canonical statement form, one consistent (Mathlib-aligned) notation across the whole graph, proofs reorganized around *our* prerequisite nodes (not a book's lemma numbers), synthesizing where multiple sources overlap. No citations in the prose. Originality comes from genuine transformation, not attribution — so these transformations are the safeguard, not optional polish.

### Content rules

- **Statement: always**, for every node — so the stitched-together document reads as continuous narrative.
- **Proof: only when not in Mathlib.** An `in-mathlib` node gets a pointer to its Mathlib declaration instead of a paraphrased proof (no rewriting proofs Mathlib already has).
- **Universal, uncited voice** — write it *as Mathlib would*: canonical statement form, one consistent (Mathlib-aligned) notation across the whole graph, proofs reorganized around *our* prerequisite nodes (not a book's lemma numbers), synthesizing where multiple sources overlap. No citations in the prose. Originality comes from genuine transformation, not attribution — so these transformations are the safeguard, not optional polish.

### Reviewers (Phase 2)

Per completed cluster (pipeline, as clusters finish), beyond the Phase-1 dimensions:
- **Faithfulness + correctness** — the paraphrased statement/proof matches the math and is sound.
- **Edge faithfulness** — each `depends_on` is real (the statement/proof genuinely uses it), including edges into Mathlib roots (held to a higher standard, per `graph-reviewer`).
- **Split correctness** — a theorem split into sub-statements actually reconstructs the original.
- **Too-close-to-source** — flag any passage tracking one book's wording/structure closely enough to read as copied.

A **holistic reviewer runs at the end of Phase 2** as well (graph-quality only — coherence, consistent granularity, root validity, coverage; *not* formalization order).

---

## 5. Tier 3 (future, planned-for)

Lean statements, hierarchical under tier-2 by the same `parent`/quotient rule. Maps onto blueprint's existing "statement formalized vs proof formalized" axes (`\lean`/`\leanok`). No schema change needed — `tier: 3` nodes with tier-2 parents. It will follow the same authority pattern (§1): take tier-2 as a scaffold, build the authoritative tier-3 with full freedom, then re-project tier-2 **and** tier-1 from it. Designing the schema and the projection generically now is what makes this drop-in.

---

## 6. Visualization — `leanblueprint`, fork-free

Prototype-confirmed. The blueprint dep-graph is a graphviz **DOT string laid out client-side by WASM** (`d3-graphviz`), embedded as a `renderDot(\`…\`)` call. That makes the tier toggle a clean DOT-swap, and requires **no Python fork**.

**Exporter** (new, ours): from `graph.json` + `content/*.md`, emit
- `blueprint/src/content.tex` — one blueprint environment per tier-2 node, with vanilla annotations;
- per-tier DOT strings in a generated `blueprint/web/tier_dots.js` sidecar (we own the data, so we generate authoritative DOTs for every tier — the quotient collapse is ours);
- a custom `dep_graph.html` (copied from `plastexdepgraph`, + a tier `<select>` that calls `renderDot(DOT[tier]).on("end", interactive)`), wired in via the supported `tpl=` package option — a template override, not a fork.

**Annotations per node** (status mapping, prototype-verified):

| our `mathlib_status` | emit | renders as |
|---|---|---|
| in-mathlib | `\lean{decls}` + `\mathlibok` | green filled box |
| partial | `\leanok` on statement, proof env open | green border |
| missing, all deps formalized | nothing | blue ("ready to formalize") |
| missing, a dep still missing | `\notready` | orange border ("blocked") |

`\uses{...}` = `depends_on`. A missing node's readiness is **computed from its dependencies** (ready iff every prerequisite is in-mathlib or partial) — there is no stored `ready` field. For blueprint's own native graph the blue is auto-derived from `\uses`; for our per-tier toggle DOTs the exporter computes the same readiness itself.

**id ↔ label:** `graph.json` keeps the full English name as the displayed title; the exporter generates a slug-safe `\label` per node (`[A-Za-z0-9_:.]`) and an internal name↔slug map for `\uses`. A pre-export pass guarantees every `depends_on` target has a `\label`.

**Cluster-node clicks:** tier-1 nodes have no statement modal, so our custom JS handles a collapsed-node click (show a cluster summary / drill into tier-2).

**In-mathlib authority stays ours:** blueprint's `checkdecls` only checks "exists in project+Mathlib," so we keep the `mathlib-checker` (+ MCP search) as the in-mathlib judge and use blueprint purely as the renderer.

The v1 `generate_graph.py` Cytoscape viewer has been removed; the blueprint exporter replaces it.

---

## 7. Toolchain & setup

Web build needs **no LaTeX** (pure-Python plasTeX). Verified install recipe:

```bash
brew install graphviz
CFLAGS="-I$(brew --prefix graphviz)/include" \
LDFLAGS="-L$(brew --prefix graphviz)/lib -undefined dynamic_lookup" \
  pip install pygraphviz          # flag only needed on nonstandard (Meta) Python
pip install leanblueprint plastexdepgraph plastexshowmore plasTeX
# view over a local HTTP server (leanblueprint serve), NOT file:// — WASM won't load otherwise
```

Recommended robust path: a **dedicated venv from a standard Python** (e.g. brew's), which builds `pygraphviz` without the linker flag. The flag is the documented fallback.

Ship:
- **`scripts/check_toolchain.sh`** (or a `/lean-informal-planner:setup` skill) — verifies `dot`, the pip packages, `import pygraphviz`, Python ≥3.10, and prints the exact fix for whatever's missing.
- **`SETUP.md`** with the recipe + venv guidance.
- The export/`plan-view` skill checks the toolchain first and points to setup on failure (graceful, no stack trace).

---

## 8. Components — reuse / revise / new

**Reuse:** MCP Mathlib server, SessionStart hook, the `mathlib-checker` (now cluster- and node-aware), `gap-finder`, `holistic-reviewer` (now also end-of-Phase-2).

**Revise:**
- `skills/plan/references/plan-json-schema.md` → rewrite for the generic N-tier model (§2).
- `skills/plan/SKILL.md` → Phase 1 coarse (§3), Phase 2 detailed (§4).
- `agents/graph-reviewer.md` → add edge-faithfulness + the existing higher-standard-for-Mathlib-roots rule.
- `skills/plan-view/SKILL.md` → build the blueprint web + open it (toolchain-checked).

**New:**
- `agents/splitter.md` — Phase 2 split + write content.
- `agents/content-reviewer.md` — faithfulness / correctness / split-correctness / too-close.
- `scripts/export_blueprint.py` — `graph.json` + `content/*.md` → `content.tex` + `tier_dots.js` + custom `dep_graph.html`.
- `templates/dep_graph.html` — our tier-toggle template (from `plastexdepgraph`).
- `scripts/check_toolchain.sh` + `SETUP.md` (+ optional `/setup` skill).

**Removed:** `scripts/generate_graph.py` (the v1 Cytoscape viewer, superseded by the blueprint exporter).

---

## 9. Build order

1. **Schema rewrite** (§2) — the data model everything else targets.
2. **Toolchain setup** — `check_toolchain.sh` + `SETUP.md`, so the viz path is runnable.
3. **Blueprint exporter + tier-toggle template** — prove the viz pipeline end-to-end on *existing* v1-style data (immediate visual payoff, and it's independent of the agent changes).
4. **Phase 1 coarse** — extraction grouping + reviewer granularity adjustment.
5. **Phase 2 detailed** — `splitter` + `content-reviewer` + the wave-parallel orchestration + main-agent merge/quotient logic.
6. **Integrate & test** — full run on a couple of chapters; confirm tiers, content, blueprint toggle.

---

## 10. Risks / open items

- **Paraphrase correctness** is the central quality risk — the content-reviewer's faithfulness+correctness pass is load-bearing.
- **`pygraphviz` install** on locked-down machines — mitigated by the venv path and the documented flag; the exporter can also set `nonreducedgraph` since we generate DOTs ourselves.
- **Wave parallelism cost** — Phase 2 is essentially writing a textbook; token cost scales with the missing/target content. Bounded by writing proofs only for non-Mathlib nodes.
- **`tpl=` path resolution** under the leanblueprint CLI's `chdir` — pass an absolute template path.
- **Cluster-node UX** — collapsed-node click behavior needs design (summary vs drill-down).
