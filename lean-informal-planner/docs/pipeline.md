# Lean Informal Planner — Pipeline

A developer-oriented description of how the plugin works: its artifacts, its subagents, how parallelism is handled, and who edits or flags what. The plugin **plans** Lean formalization from textbooks — it builds a tiered dependency graph of concepts mapped to Mathlib; it does not formalize. (The mathematical criteria the reviewers apply live in the individual agent definitions under `agents/`; this document covers the architecture.)

The pipeline runs in two phases, each ending in review, all driven by one top-level agent coordinating a small set of specialized subagents.

---

## 1. Artifacts

A plan lives in the user's project directory (next to `lakefile.toml`):

- **`graph.json`** — the structure. One JSON object: metadata plus a map of nodes keyed by `id` (the concept's full English name). Each node holds only *structural* fields: its tier, its container (`parent`), its within-tier dependency edges (`depends_on`), its Mathlib correspondence, and a pointer to its prose file. Derived data is never stored here (see below).
- **`informal_content/<id>.md`** — the prose. One Markdown file per node (statement, plus proof when the concept is not in Mathlib).
- **`sources/`** — the textbooks. The orchestrator moves received books here; `metadata.sources` records their paths.

Three helper scripts under `${CLAUDE_PLUGIN_ROOT}/scripts/`: **`merge_node.py`** (the locked writer for `graph.json`), **`check_invariants.py`** (structural checker), **`export_blueprint.py`** (renders the plan to an interactive web view). Full data model: [`skills/plan/references/plan-json-schema.md`](../skills/plan/references/plan-json-schema.md).

### Two design decisions in the data model

- **Structure and prose are separate files.** `graph.json` stays small, readable, and diffable; the bulky prose lives one-file-per-node in `informal_content/`. Subagents writing prose touch only their own files; all writes to the structure go through one locked path (`merge_node.py`).
- **Coarser tiers are derived, not stored.** The only authored hierarchy link is each node's `parent`. A cluster's membership is read off the `parent` pointers, and a tier-1 edge A→B exists exactly when some tier-2 node in A depends on one in B (the *quotient* rule). The finest built tier is the source of truth; coarser tiers are recomputed from it, so they cannot drift — and many subagents can build the fine graph in parallel while the coarse graph stays consistent by construction.

The tiers: **tier 1** = coarse concept clusters (the scoping map), **tier 2** = fine definitions/statements with content, **tier 3** = Lean statements (planned, not built).

---

## 2. The orchestrator

The top-level agent — **the orchestrator** (a.k.a. the main agent) — runs the whole pipeline: it dispatches subagents, threads results between them, merges splitter output into the graph, applies holistic fixes, reverts rejected changes, and talks to the user.

`graph.json` has a **single write *path*, not a single writer**: every structural change goes through `merge_node.py`, which is file-locked and atomic. Both the orchestrator and the editing `graph-reviewer`s call it, and the lock serializes their concurrent writes, so changes never race or corrupt the file. Prose is separate — each `splitter` and `content-reviewer` writes its own `informal_content/` files directly, and since those are disjoint they need no shared writer.

---

## 3. The two phases

**Phase 1 — coarse (tier-1) graph.** A cheap, user-reviewable scoping map that also hands Phase 2 its starting partition.
1. **Extract** the book's mathematics into coarse clusters, each a tier-1 node (with a provisional list of the statements it will later contain).
2. **Check and ground:** one `mathlib-checker` per cluster (parallel); ground every cluster down to green (`in-mathlib`) roots.
3. **Review** in two waves (§5).
4. **Export and approve:** render to the blueprint view; the user signs off before Phase 2.

**Phase 2 — detailed (tier-2) graph.** The tier-1 graph is a *scaffold*; tier-2 is built and becomes the source of truth.
1. **Split**, as a continuous pool (§4): each cluster goes to a `splitter` once its prerequisite clusters are split. The splitter produces the cluster's tier-2 nodes (structure) and writes their prose.
2. **Persist:** the orchestrator merges each splitter's structure into `graph.json` as it lands, and runs a `mathlib-checker` on fresh status guesses.
3. **Review — Wave A** (§5).
4. **Re-project tier-1** from the finished tier-2: node set and edges recomputed mechanically (the quotient), metadata re-curated.
5. **Review — Wave B**, then export the full tiered blueprint.

The same scaffold → build → re-project pattern will apply when tier 3 arrives.

---

## 4. Parallelism

Much of the work is a **DAG of tasks** — partly parallel, partly dependent (Phase-2 splitting is the prime case). The scheduling decisions:

- **Continuous dispatch, not fixed batches.** A task launches the moment its prerequisites are done; a freed worker immediately takes the next ready task. In ultracode this is a *promise pool* inside a workflow; in plain mode it's background subagents collected as they finish. One slow subagent blocks only its own dependents, never unrelated work.
- **The task set emerges.** New tasks appear as the work reveals them (a splitter discovers a prerequisite; a reviewer finds a missing node). Readiness is re-derived from the current graph after each completion, not from a plan fixed up front.
- **Genuine synchronization points are kept.** Some steps need everything before them — re-projecting tier-1, the two review *waves*, the Phase-1 approval gate. Those wait on purpose; only incidental batching barriers are avoided.
- **One write path.** Every structural change funnels through `merge_node.py`, so concurrent writers serialize on it, results persist as they land, and no agent holds the whole graph in memory.

**Partitioning is responsibility, not visibility.** When the graph is large, a review task is split across several reviewers. Each gets the **list of node ids it is responsible for editing** — but **full read access to everything** (the whole `graph.json`, any node's prose, the sources, Mathlib), and is told to read as much surrounding context as the task needs. The partition bounds what each agent *edits* (so concurrent edits stay conflict-free), never what it can *see*. A reviewer edits only its own nodes' records; anything outside its responsibility it **flags** for the orchestrator.

**Convergence.** Each review wave loops until convergence — a round that makes no substantive change (editing wave) or surfaces no new significant issue (holistic wave) — or until the orchestrator judges progress has clearly stalled.

---

## 5. Review: who edits, who flags

Both phases review in **two waves** — an editing wave that fixes the graph in place, then a holistic wave that judges the whole.

**Wave A — editing reviewer-correctors:**
- **`graph-reviewer`** (both phases) owns the dependency *structure* (edges, redundant nodes, missing intermediates). It **edits `graph.json` directly** through `merge_node.py` for the nodes it owns, runs partitioned when the graph is large, and flags anything outside its responsibility.
- **`content-reviewer`** (Phase 2), one per cluster, owns the *prose*. It **edits the `informal_content/<id>.md` files directly** and flags structural problems for the graph-reviewers. Clusters are disjoint, so these run concurrently.

**Wave B — holistic reviewers (flag-only):** at least three `holistic-reviewer`s run independently in parallel over the entire graph. They **do not edit**; they surface corrections to the orchestrator, which applies small fixes directly and dispatches a targeted `graph-reviewer` for larger structural ones.

**Guard rails around editing.** Before each wave the orchestrator snapshots `graph.json` (enabling whole-wave rollback); after each wave it runs `check_invariants.py` and fixes any offender or rolls back. Each editing reviewer returns a **concise change-report** (the concrete changes it made), so the orchestrator keeps a bounded view of a large run and can revert any change it rejects.

---

## 6. Subagent roster

| Subagent | Phase | Role | Edits or flags | Model |
|---|---|---|---|---|
| **`mathlib-checker`** | both | Classifies one concept against local Mathlib. | Returns data (orchestrator merges) | sonnet |
| **`splitter`** | 2 | Splits one cluster into tier-2 nodes; writes their prose. | **Edits** `informal_content/`; returns structure | opus |
| **`graph-reviewer`** | both | Reviews/corrects dependency structure over its partition. | **Edits** `graph.json` (via `merge_node.py`); flags the rest | opus |
| **`content-reviewer`** | 2 | Reviews/corrects one cluster's prose. | **Edits** `informal_content/`; flags structural issues | opus |
| **`holistic-reviewer`** | both | Whole-graph quality; ≥3 run in parallel. | **Flags only** | opus |
| **`source-searcher`** | both | Fetches a specific result from a book, so whole books stay out of the orchestrator's context. | Returns extract | sonnet |
| **orchestrator** | both | Drives phases; dispatches and threads subagents; merges structure; applies/reverts; talks to the user. | Writes `graph.json` via `merge_node.py` | — |

In short: **splitter**, **graph-reviewer**, and **content-reviewer** change the plan (prose, or structure via the writer); **mathlib-checker**, **holistic-reviewer**, and **source-searcher** only return findings; and every write to `graph.json` — by the orchestrator or an editing `graph-reviewer` — goes through `merge_node.py`.

---

## 7. Determinism and safety

- **Single write path.** Every write to `graph.json` goes through the locked, atomic `merge_node.py` — used by the orchestrator and the editing `graph-reviewer`s — so concurrent writers serialize and never corrupt the file.
- **Structural check.** `check_invariants.py` verifies the global invariants a partitioned reviewer cannot see — reference integrity, tier discipline, per-tier acyclicity, every `missing` node reaching an `in-mathlib` root — after each review wave.
- **Snapshots and revert.** A pre-wave file copy allows whole-wave rollback; per-change reports let the orchestrator reverse any individual edit.
- **Derived-not-stored.** Membership and coarse edges are recomputed from the fine graph, so the tiers cannot drift.
- **Context discipline.** Reviewers read on demand rather than holding the whole graph; the orchestrator delegates book searches to `source-searcher`; structure (`graph.json`) and bulky prose (`informal_content/`) are kept apart.
