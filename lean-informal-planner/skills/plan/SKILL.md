---
name: plan
description: >
  This skill should be used when the user asks to "plan a formalization",
  "build a dependency graph", "map concepts to Mathlib", "analyze a textbook
  for formalization", "create a formalization plan", "chart mathematical concepts",
  or wants to plan Lean 4 formalization work from textbook sources.
version: 0.2.0
---

# Formalization Planning

Turn one or more textbooks into a **tiered dependency graph** that maps the mathematics onto Mathlib and carries its own content. The plan is built in two phases. Phase 1 produces a coarse map of concept clusters — a cheap scoping checkpoint. Phase 2 splits each cluster into fine definitions and statements, each written out in a universal, Mathlib-aligned voice. The result is `graph.json` plus an `informal_content/<id>.md` file per fine node, both rendered as an interactive blueprint.

The graph is **generic over tiers**: tier 1 is coarse concept clusters, tier 2 is fine definitions and statements, and tier 3 (Lean statements) is planned for but not yet built. Every node names its `tier` and a single `parent` one tier up; everything coarser — a node's members, and the edges between clusters — is *derived* by the quotient rule, never stored. For the full data model, see `references/plan-json-schema.md`; this skill defers all schema detail there.

## Guiding Principle

**Textbooks are the source of truth.** Read and reference the provided material rather than relying on training knowledge. When a concept's prerequisites are unclear from the available material, ask the user for additional reference books rather than guessing. Be explicit about uncertainty. The only place training knowledge is appropriate is for well-established background facts (e.g. "a group homomorphism requires groups").

Before starting, confirm the sources and scope with the user: which textbook(s), in which formats (LaTeX, Markdown, PDF), and which chapters or sections to cover. If a phase needs material the provided books don't cover, pause and ask for the specific topic rather than inventing prerequisites.

When the main agent (also called the orchestrator) receives a book, it moves the file into a `sources/` subfolder of the plan directory and records its path there in `metadata.sources`. When it later needs a specific result or definition from a book — to verify an edge, ground a prerequisite, or resolve a question — it spawns a `source-searcher` subagent to search the book and return just the needed extract, keeping the book out of its own context.

---

## Parallelism

Many tasks across this project form a **DAG** — partly parallelizable, partly dependent. Phase-2 splitting is the main case, but the pattern recurs (per-concept Mathlib checks, reviews, future tiers). Whenever you launch many subagents on such a task set, schedule it like this:

- **Maximize concurrency under the dependencies.** Run everything that *can* run at once, at once; let only real prerequisites hold a task back.
- **Dispatch continuously, not in waves.** Use a Promise pool in a workflow (ultracode), or background subagents collected as they finish in plain mode. Reassess on **each completion** and launch whatever just became ready — rather than `parallel()`-style batches, where the whole batch waits on its slowest member.
- **Keep genuine synchronization points.** When the next step truly needs *all* prior results — re-projecting tier-1 from a finished tier-2, deduping across all reviewer findings, a user-approval gate — wait for them. Avoid only *incidental* barriers, not necessary ones.
- **Let the task graph emerge.** New tasks appear as the work reveals them — a splitter discovers a prerequisite, proposes a new cluster. Re-derive what's ready from the current graph after each completion and launch a task once it becomes ready, rather than from a plan fixed in advance.
- **Persist each result as it lands, through a single writer.** Fold each completed result into `graph.json` as it returns. One writer keeps the file consistent, the run stays durable as it grows, and memory stays bounded because each merge touches only that result.
- **Intervene on a stuck subagent when warranted.** A runaway or hung subagent can be stopped with `TaskStop` and relaunched or flagged. This is per-subagent in plain mode; inside a workflow only the whole run can be stopped, so reserve that for a genuine stall.

---

## Phase 1: Build the coarse (tier-1) graph

Phase 1 produces a high-level map: the textbook's mathematics grouped into coherent concept clusters, each grounded down to Mathlib. It is deliberately coarse — a quick, user-reviewable picture of scope that also hands Phase 2 its starting partition (top-down splitting of known clusters is far more tractable than clustering fine nodes bottom-up).

### 1a. Extract coarse clusters

Read the provided textbook(s) across the user's scope — for PDFs, read visually in chunks of ~20 pages and work systematically through the document.

**Group the material into coarse concept clusters.** Each cluster is a coherent topic: a high-level concept together with its associated results. Granularity tracks **mathematical weight**, not a fixed count — a major theorem can stand as its own cluster, while a family of minor results belongs together in one. Use the book's section structure as a starting guide, merging or splitting sections wherever that yields more coherent topics.

For each cluster, record as a tier-1 node:
- An `id` equal to the cluster's ordinary English name, written out in full (e.g. `Concentration inequalities`, `Sub-Gaussian variables`). Book numbering belongs in `source_refs`, never the id.
- Its `kind`, a short description, and its `source_refs`.
- A provisional list of the statements it will contain — names only. These become tier-2 nodes in Phase 2 and are not yet authored as nodes; they are the cluster's contents-to-be, carried alongside for Phase 2's benefit.

Write the clusters into `graph.json` as tier-1 nodes (`tier: 1`, `parent: null`), per the schema reference.

### 1b. Check and ground

This step operates at **cluster grain**:

- **Mathlib check.** Fan out one `mathlib-checker` per cluster in parallel. Each returns `mathlib_status`, `mathlib_declarations`, `mathlib_file`, and `mathlib_notes`. A whole cluster is usually `partial` — some of its results are formalized, some are not.

- **Ground to green roots.** Every cluster must trace, through its `depends_on` edges, down to green (`in-mathlib`) roots. Where a prerequisite is ordinary Mathlib material, ground it in a coarse green node at roughly the granularity of a Mathlib topic folder (e.g. `Analysis/Calculus/Gradient`) — a guideline, not a rule: merge folders that are too thin, split ones that sprawl. Create such a node only after you have actually found the prerequisite with `mathlib_grep` or `mathlib_find_name`, name it after the common subfolder of the hits, and record the declarations the search returned. Never draw an edge into Mathlib without a verified declaration behind it; when you cannot find the prerequisite, leave the node `missing` or ask the user for reference material.

  During Phase 1 the tier-1 graph is **authored directly** (there are no tier-2 nodes yet to derive it from). It is a scaffold: Phase 2 builds the authoritative tier-2 graph and then re-projects tier-1 from it, so this coarse graph guides the next phase rather than binding it.

### 1c. Review in two waves

The coarse graph passes through two review waves. Before each wave the orchestrator snapshots `graph.json` (a plain file copy) so a whole wave can be rolled back if it goes wrong, and after each wave it runs the deterministic structural check `scripts/check_invariants.py` — per-tier acyclicity, every `missing` node reaching an in-mathlib root, no dangling `depends_on`/`parent` references — invariants a partitioned reviewer cannot see on its own.

**Wave A — editing reviewer-correctors.** Run `graph-reviewer` over the coarse graph. Its remit is edge correctness, edge faithfulness, missing edges, spurious edges, redundant nodes, **and gap-finding** — the missing intermediate clusters. It edits `graph.json` directly through `scripts/merge_node.py`. When the graph is large, partition it and run one reviewer per subset. The partition is **responsibility only, not visibility**: each reviewer is given the list of node ids it is responsible for, plus full read access to everything (`graph.json`, `informal_content/`, `sources/`, Mathlib), and is encouraged to read as much surrounding context as the task needs, with "as needed" the governor. `graph.json` is itself the index; no precurated index is supplied. A reviewer edits only its own nodes' records, hence only those nodes' outgoing `depends_on` edges; anything outside its responsibility — a duplicate elsewhere, a node elsewhere that should depend on one of its nodes, a cross-partition merge — it **flags** in its report. Within-partition merges it performs by sending a complete payload (re-point neighbours to the survivor, then delete the absorbed node). Each reviewer returns a **concise change-report** enumerating the concrete changes it made (one line plus justification each) and its flags, so the orchestrator keeps a bounded global view and can revert any rejected change with a compensating `merge_node.py` edit. Run a `mathlib-checker` on any newly added cluster. Loop Wave A until convergence (or until progress has clearly stalled).

**Wave B — holistic reviewers (flag-only).** Launch at least 3 `holistic-reviewer`s independently in parallel over the entire graph. They do not edit; they surface corrections to the orchestrator, which applies them — small or local fixes directly via `merge_node.py`, larger structural fixes by dispatching a targeted `graph-reviewer` on the affected region. `holistic-reviewer` judges **graph quality only** — overall coherence, granularity that is consistent by significance, valid Mathlib roots, and coverage gaps the specialized reviewers missed. Loop until convergence (or until progress has clearly stalled).

Throughout, apply high-confidence corrections and note each change; surface uncertain or conflicting suggestions to the user with the reviewers' reasoning and ask for a decision.

### 1d. Export and view

Export and open the blueprint for the user to review (`/plan-view`). Phase 1 ends here, with a user-approved coarse map. Confirm the user is happy with the scope before moving on — correcting a cluster now is far cheaper than after it has been split.

---

## Phase 2: Build the detailed (tier-2) graph

Phase 2 splits each tier-1 cluster into its fine definitions and statements and writes the mathematical content for each. The **tier-1 graph is a scaffold, not a cage**: it guides the work, but tier-2 is what's being built and becomes the source of truth. Splitters may add tier-2 nodes a cluster didn't anticipate, drop ones that don't belong, re-parent, and add edges across clusters or to newly discovered prerequisites. Once tier-2 is stable, tier-1 is **re-projected** from it (step 2e). This phase is essentially writing a textbook in a single consistent voice, so it walks the graph in order and parallelizes carefully.

### 2a. Split the clusters as a continuous pool

Process the tier-1 scaffold as a **continuous, dependency-ordered pool**, following the Parallelism section. Treat the graph itself as the live worklist: a cluster is *ready* to split once its prerequisite clusters are split (so its splitter can reference their tier-2 nodes by id). Seed the pool with the clusters that have no unsplit prerequisites, launch a splitter for each, and on **each completion** merge the result (2c) and launch any cluster that has just become ready.

The ordering is **best-effort**: where possible a node's prerequisites are written before it (so its prose can be self-contained) and shared prerequisites aren't created twice. The full split order emerges as you go — clusters and prerequisites are *discovered* as splitters run. When a splitter discovers a prerequisite that isn't present yet, it adds the node and edge immediately and references it by id; that node is split or given content when it is reached. Re-derive readiness from the current graph after every completion, and track which nodes still await content.

### 2b. Launch one splitter per ready cluster

Launch one `splitter` per ready cluster, continuously, as the pool frees up. Each `splitter` receives its cluster, the textbooks, and a trimmed index of its prerequisite clusters' tier-2 nodes (`id` plus one-line description) — threaded into its prompt at launch, so it can draw correct cross-cluster edges without reading the whole graph. It:

1. Splits the cluster into tier-2 nodes — one per definition or statement; a hard theorem may become a few sub-statements, where the split is a claim that the pieces recompose to the original. The cluster's `provisional_members` from Phase 1 are a hint, not a contract: add, drop, or regroup freely.
2. Sets each tier-2 node's `parent` and within-tier `depends_on` (referencing prerequisite nodes by id), and proposes a `mathlib_status` guess for each. A node that fits no existing cluster gets `parent: null` for now, or names a proposed new cluster; the re-projection step settles its home.
3. Writes `informal_content/<id>.md` for each node, following the content rules below.
4. Returns the new, changed, and deleted nodes, edges, and content to the main agent (also called the orchestrator), flagging any tier-1 issue the split reveals.

A `splitter` returns structural data and writes only its content files; `graph.json` is the orchestrator's to write.

### 2c. Persist each split through the single writer

`graph.json` has exactly **one writer**, and each split is folded in **as it lands**, one result at a time. As each `splitter` returns, the orchestrator:

- Merges its new, changed, and deleted tier-2 nodes and edges into `graph.json` through the deterministic merge writer (`scripts/merge_node.py`). The writer keys nodes by id, strips any edge left pointing at a removed node, and writes under a lock so concurrent splitters serialize on the file. It is mechanical and runs outside any agent's context, so persisting a result costs nothing as the graph grows.
- Tracks which nodes still await content (forward references from later-discovered prerequisites).
- Runs a `mathlib-checker` on any node whose status is a fresh guess.

It does **not** recompute tier-1 or the derived coarse edges on each merge — those are a function of the whole tier-2 graph and are rebuilt once, at re-projection (2e). An incremental tier-2 merge needs only the single node record, not the graph in memory.

### Content rules

Each `informal_content/<id>.md` is written in a **universal, uncited voice — as Mathlib would write it**:

- **Statement: always**, for every node, so the stitched-together content reads as one continuous narrative.
- **Proof: only when not in Mathlib.** An `in-mathlib` node gets a pointer to its Mathlib declaration in place of a paraphrased proof — never rewrite a proof Mathlib already has. A `partial` or `missing` node carries a full proof.
- Use **one consistent, Mathlib-aligned notation** across the whole graph, give each statement in its canonical form, and reorganize proofs around *our* prerequisite nodes rather than any book's lemma numbering, synthesizing where multiple sources overlap. No citations appear in the prose; `source_refs` is internal bookkeeping for faithfulness-checking and is never rendered. Originality comes from genuine mathematical transformation, not from attribution — these transformations are the safeguard against copying, not optional polish.

### 2d. Review — Wave A

Wave A runs two editing reviewer types together over the tier-2 graph. As in Phase 1, the orchestrator snapshots `graph.json` before the wave and runs `scripts/check_invariants.py` after it.

- **`content-reviewer`s, one per cluster, editing.** Each reviews a cluster's tier-2 prose for **faithfulness and correctness** (the paraphrased statement and proof match the mathematics and are sound), **split correctness** (a theorem split into sub-statements reconstructs the original), **too-close-to-source** (flagging any passage that tracks one book's wording or structure closely enough to read as copied), and the **in-mathlib pointer checks**, and fixes what it finds by editing the `informal_content/<id>.md` files directly. Structural issues — wrong edges, misplaced nodes — it **flags** for the graph-reviewers / orchestrator rather than touching `graph.json`. Clusters are disjoint, so these run concurrently. Loop until convergence (or stall).
- **`graph-reviewer`s, editing — the same agent and rules as Phase 1 Wave A**, with the remit adapted to tier-2 edges: edge correctness, edge faithfulness (each edge genuinely used by the statement or proof), missing and spurious edges, redundant nodes, and gap-finding. Holds edges into Mathlib roots to a higher standard, verifying the specific prerequisite declaration exists. Responsibility-not-visibility partition when the graph is large, concise change-reports, revert on rejection, loop to convergence-or-stall. **Gap-finding applies in Phase 2 too:** when a reviewer catches a missing intermediate tier-2 node (an omission a splitter missed, or a cross-cluster jump), it creates the structural node and edges via `merge_node.py` and marks it content-pending (`content: null`); the "nodes awaiting content" step (2c) then has its prose written.

Apply high-confidence corrections and note each change; surface uncertain or conflicting suggestions to the user with the reviewers' reasoning.

### 2e. Re-project tier-1 from tier-2

Once tier-2 is stable, rebuild tier-1 from it — tier-2 is now the source of truth, and the Phase-1 tier-1 was only ever a scaffold. This has a mechanical part and a curated part:

- **Mechanical.** The tier-1 node set is the distinct `parent` values of the tier-2 nodes: prune clusters left empty, and materialise a cluster for every orphan (`parent: null`) or newly proposed parent. Tier-1 edges are the quotient of the tier-2 edges (cluster A → B iff some tier-2 node in A depends on one in B).
- **Curated.** A cluster's metadata is *not* mechanically derived — re-curate it to match the new membership: name any new cluster, update descriptions, roll up `mathlib_status`, and merge or split clusters whose composition no longer coheres. Run `graph-reviewer` and `holistic-reviewer` over the re-projected tier-1 so it stays a meaningful coarse map rather than decaying into a bare rollup.

(The same scaffold → build → re-project pattern will apply when tier 3 arrives: build tier-3 from tier-2, then re-project tier-2 and tier-1 from it.)

### 2f. Review — Wave B, then export and view

Wave B is the holistic wave, same rules as Phase 1: snapshot `graph.json` first, launch at least 3 `holistic-reviewer`s independently in parallel over the complete two-tier graph, and run `scripts/check_invariants.py` after. They judge **graph quality only** — coherence, granularity consistent by significance, root validity, and coverage (not formalization order) — and are flag-only: the orchestrator applies their corrections, small or local fixes directly via `merge_node.py`, larger structural fixes by dispatching a targeted `graph-reviewer` on the affected region. Loop until convergence (or until progress has clearly stalled).

Then export and open the full tiered blueprint via `/plan-view`, where the tier toggle reveals the coarse map and the fine graph with content.

---

## Agent Usage

The roster, by phase and capability:

- **`mathlib-checker`** — reused as-is in both phases. Input a concept (name + description + kind); output `{mathlib_status, mathlib_declarations, mathlib_file, mathlib_notes}`. The task is identical whether the concept is a tier-1 cluster or a tier-2 node. Fan out one per concept in parallel; these are embarrassingly parallel, and the cheaper model suffices.
- **`splitter`** — new, Phase 2. Splits one tier-1 cluster into tier-2 nodes, writes their content, and returns structural data for the orchestrator to merge; flags any tier-1 error the split reveals. Run one per ready cluster, launched continuously as the pool frees up.
- **`content-reviewer`** — Phase 2, Wave A, **editing**. Reviews one cluster's tier-2 content for faithfulness, correctness, split correctness, too-close-to-source, and in-mathlib pointer checks, and fixes what it finds by editing the `informal_content/<id>.md` files directly. Flags structural issues for the graph-reviewers rather than touching `graph.json`. One per cluster, run concurrently, looping to convergence-or-stall.
- **`graph-reviewer`** — Wave A in **both phases**, **editing and gap-finding**. Reviews dependency edges for correctness and faithfulness, finds missing and spurious edges and redundant nodes, and catches missing intermediate clusters/nodes — at whatever tier and scope it is given (coarse edges in Phase 1, tier-2 edges in Phase 2). Edits `graph.json` through `merge_node.py`, under a responsibility-not-visibility partition when the graph is large, returning a concise change-report and flagging anything outside its responsibility. Treats coarser derived edges as read-only and holds edges into Mathlib roots to a higher standard. Loops to convergence-or-stall.
- **`holistic-reviewer`** — Wave B in **both phases**, **flag-only**. Judges overall graph quality (coherence, granularity by significance, root validity, coverage) — not formalization order. At least 3 run independently in parallel over the whole graph; the orchestrator applies their findings. Loops to convergence-or-stall.
- **`source-searcher`** — utility, either phase. Given a book and a specific result or definition to find, it searches the book and returns just the needed extract, so the orchestrator can resolve a question without reading the book into its own context.

When running with workflow orchestration (ultracode), use `parallel()` for the genuinely independent fan-outs — the per-concept `mathlib-checker` checks and the parallel reviewers — where the next step needs them all. Run the `splitter`s as a **continuous Promise pool** over the cluster graph (per the Parallelism section), launching each as its prerequisites complete rather than in `parallel()` batches. Without orchestration, the main agent drives the same readiness loop with background subagents, collecting each as it finishes — same shape, lower throughput.

## Self-Critique Protocol

All agents include self-critique instructions. When processing agent results, check for a `## ⚠️ Issue` section at the top of their output. If found:
- Surface the issue to the user immediately, including the agent's suggested improvements.
- Ask whether to proceed, adjust, or provide additional reference material.

## Additional Resources

For the full data model — the two-file encoding, every node field, the hierarchy invariant, the list of invariants, and a worked two-tier example:
- **`references/plan-json-schema.md`** — Complete v2 plan schema reference.
