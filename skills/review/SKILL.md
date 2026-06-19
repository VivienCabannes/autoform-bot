---
name: review
description: >
  This skill should be used when the user asks to "review" a node / cluster / the
  formalization, "build the reviewer packet", "open the review UI", "show the review
  graph", "check faithfulness of the statements", "score the formalization", or set
  the review dial. DAG-native human-review surface over the tiered plan: a headless
  text reviewer packet by default, or a local review UI with `--view`.
version: 0.1.0
---

# Review — the human-review surface

A formalization is only trustworthy when a human Lean expert will vouch for the
**statements** (the kernel already vouches for the proofs). This skill produces the
artifacts that make that vouching fast and decisive, **DAG-native** over the tiered
plan (`graph.json` + the built blueprint), keyed by node `id`.

Two outputs from one skill:

- **`review <id>`** (default, headless, CI-friendly) — emit the **text reviewer
  packet** for a node or cluster: spec sheet + kernel evidence + jury scorecard. No
  server; good for agent loops and CI.
- **`review --view`** — build the blueprint **if stale**, then launch the **local
  review UI** on `127.0.0.1` and open the recolored dependency graph.

Both read the same sidecar, `review_status.json` — the single source of truth for
verdicts, and **the only file this surface ever writes**. `graph.json` and
`informal_content/` stay pristine.

The packet's structure, trust-class taxonomy, and the rules that make it honest live
in `references/reviewer-packet.md` — read it before producing any packet.

## Two encodings on the graph (never conflate them)

- **Position = `mathlib_status`** — vertical lanes: `in-mathlib` at the **bottom**
  rising to `missing` at the **top**; dependencies flow **upward** (a frontier
  theorem sits above the grounded lemmas it rests on).
- **Color = the effective review verdict** — green clean / amber flagged / red
  rejected / grey unreviewed.

## Two review sources per node (sidecar slots `ai` + `human`)

- **`ai`** — the **weighted jury** (see the `eval-rubrics` skill): three blind
  single-axis judges — `faithfulness`, `proof_integrity`, `code_quality` (0–5 each).
  Displayed score = `0.40·faith + 0.40·integ + 0.20·qual`. Verdict is
  **threshold-gated, not the average**: **clean** = all pass (faith ≥4, integ ≥3,
  qual ≥3); **rejected** = faith ≤2 **or** integ ≤2; **flagged** = otherwise. Style
  alone never rejects.
- **`human`** — verdict (clean/flagged/rejected) + 0–5 score + note + by/at.
- **Effective verdict = `human` if present, else `ai`.** Human is **immutable**:
  re-running the AI rewrites only the `ai` slot, never the human one. AI-only nodes
  render with a **dashed ring**; human-confirmed render **solid** (so an
  AI-greened-but-unvouched node reads as provisional).

The jury is **always on** and **incremental**: a node is judged when created or when
its statement/proof changes; a recorded human verdict freezes it. The jury does not
re-sweep the whole DAG on every run.

## Roll-up, taint, trust frontier (computed live, never stored)

- **Tier-1 cluster roll-up** — a cluster is clean **only if every** tier-2 child is
  clean; any flagged/rejected child ⇒ cluster flagged.
- **Taint** — a flagged/rejected node **hatches its entire downstream `depends_on`
  closure** (forward reachability from the bad node to everything that depends on
  it), recomputed live on every read and after every verdict write.
- **Trust frontier** — the sink nodes (top-level results) whose entire `depends_on`
  closure is fully clean. These are the results a human can currently trust
  end-to-end.

All of these are pure functions of (`graph.json`, `review_status.json`) implemented
in `${CLAUDE_PLUGIN_ROOT}/scripts/review_ui/review_model.py` (`verdict_of`,
`tainted_set`, `cluster_rollup`, `coverage`, `trust_frontier`, `recolor_dot`).

## The dial (spec-generation level)

The jury **always** colors the whole DAG; the **dial governs spec (review-artifact)
generation only**, stored in `review_status.json` → `settings.dial`, per-project and
remembered (no per-run prompt — switch by asking):

| setting | jury (colors DAG) | specs auto-generated for |
|---|---|---|
| **on-demand** *(default)* | always | none — built only on request |
| **targets** | always | sink/target nodes only (= the spec-gate) |
| **full** | always | the whole DAG |

Specs are built **bottom-up** (finest tier t2/t3 first), then assembled into
**review decks** grouped by tier-1 cluster. A *spec* = a node's reviewer packet; a
*review deck* = the tier-1 cluster bundle (the cluster drill-down screen). To change
the dial, set `settings.dial` in `review_status.json` to one of the three values.

## Headless: `review <id>` (the text packet)

Resolve `<id>` to a node or a tier-1 cluster in `graph.json`, then emit the packet:

1. **Spec sheet** — for the node (or each child of a cluster): the Lean statement
   (verbatim signature) · the source statement (verbatim, from `source_refs`) ·
   one-sentence plain-math meaning · source citation · trust class
   (`DEF`/`STMT`/`INSTANCE`/`NOTATION`/`PROOF`/`AXIOM`/`SORRY`). Order so the
   must-read lines come first; state the must-read line count. (`reviewer-packet.md`.)
2. **Kernel evidence** — paste real output, never a summary: `#print axioms <decl>`
   per `mathlib_declarations`, reported as a **delta vs base** (`propext`,
   `Classical.choice`, `Quot.sound`); a word-boundary grep for introduced
   `sorry`/`admit`/`axiom`. If a `kernel/<id>.txt` dump exists, include it verbatim.
3. **Jury scorecard** — the three rubric scores from the `ai` slot, the weighted
   total, and the threshold-gated verdict, plus the effective verdict (human if
   present). A packet with an unexplained `AXIOM`/`SORRY` row is a **failed** packet.
4. For a **cluster id**, assemble the children's specs into the **review deck** and
   print the roll-up (clean only if every child clean).

This default is text-only and writes nothing. It is the CI/agent path.

## `review --view` (the local UI)

1. **Build the blueprint if stale.** The UI injects the *built* `div.thm#<slug>`
   fragments (MathJax already run) — it never regenerates the informalization. If the
   blueprint under `blueprint_export/blueprint/web/` is missing or older than
   `graph.json`, build it first by delegating to the **`plan-view`** skill's steps
   (check toolchain → `export_blueprint.py` → `make web`). Do not call `plastex`
   directly.

2. **Launch the server** (stdlib, binds `127.0.0.1` only):

   ```bash
   python ${CLAUDE_PLUGIN_ROOT}/scripts/review_ui/serve_review.py \
       --graph <path/to/graph.json> [--port 8765] [--open]
   ```

   Then open `http://127.0.0.1:<port>/`. The three screens:
   - **Home `/`** — the dep-graph recolored by effective verdict (AI-only dashed ring,
     human solid, tainted hatched, `mathlib_status` vertical lanes), with the coverage
     bar + trust-frontier header + legend.
   - **Cluster `/cluster/<id>`** — a tier-1 cluster's tier-2 children + statuses + the
     roll-up (the review deck).
   - **Node `/node/<id>`** — the packet: the rendered blueprint theorem env (left)
     beside `source_refs` (verbatim) + `mathlib_declarations` + the kernel-evidence
     card + the jury scorecard; the bottom **verdict panel** writes the `human` slot
     via `POST /api/verdict/<id>` and the home graph re-taints live.

   The server reads `graph.json`, `informal_content/`, the built blueprint, an
   optional `kernel/<id>.txt`, and `review_status.json`; it **writes only**
   `review_status.json`.

## Spec-gate (targets) — faithfulness on the DAG roots

Setting the dial to **targets** runs the `faithfulness` rubric on the sink/target
nodes (the project's main results) — the highest-leverage check, since the whole
graph hangs off the targets. A failed target is a **flagged root node** in the same
sidecar (advisory by default; surfaced in the UI filtered to roots). No separate
status, no new infrastructure — it reuses the same jury + sidecar + packet.

## When to use

- After the worker writes or changes statements/proofs — review the affected nodes.
- When a human wants to vouch for the formalization (open `--view`, walk the
  frontier, confirm or flag nodes).
- In CI / agent loops — `review <id>` for a quick, writeless packet + scorecard.
- To change the review level — set `settings.dial` in `review_status.json`.

## Rules

- Never claim "compiles" / "axiom-clean" without the command output in the packet.
- A packet with an unexplained `AXIOM`/`SORRY` row is a **failed** packet — say so.
- The surface writes **only** `review_status.json`. Never edit `graph.json`,
  `informal_content/`, or the built blueprint from the review path.
- Human verdicts are immutable — re-running the jury never overrides a human slot.
