---
name: eval-rubrics
description: >-
  Use when grading, scoring, or reviewing a Lean 4 formalization against a source text — judging
  whether a Lean statement faithfully captures a source statement, whether a proof is genuine, or
  whether code follows Mathlib style. Provides the autoform jury rubrics (faithfulness,
  proof_integrity, code_quality) with weights, pass thresholds, and the threshold-gated verdict
  mapping (clean / flagged / rejected) that drives the `ai` slot of the review_status.json sidecar.
  Use for any "evaluate / rate / is this formalization good" request on Lean code, and as the
  shared rubric source for the three single-axis reviewer agents.
---

# Evaluation rubrics for formalized mathematics

The grading criteria the AI jury applies to a formalized node. The jury is **three blind
single-axis reviewers**, each given ONLY its own rubric — never the others'. Each rubric is an
integer score 0–5 with a pass threshold; the criteria and prompt templates are the JSON files in
`references/`.

## The three jury rubrics

| Rubric | Weight | Pass ≥ | Reviewer agent | Judges whether… |
|---|---|---|---|---|
| **faithfulness** | 0.40 | 4/5 | `autoform-reviewer` | the Lean **statement** captures the source statement *at full strength* (no weakening, no vacuity) |
| **proof_integrity** | 0.40 | 3/5 | `proof-integrity-reviewer` | the **proof** chain is genuine work on sound foundations (axioms clean, no disguised `sorry`/cheats) |
| **code_quality** | 0.20 | 3/5 | `content-reviewer` | the code follows Mathlib conventions and idiomatic Lean 4 (yardstick = the **lean-conventions** skill) |

There are exactly three rubrics on the review path. The cheating-hunt splits along the two
correctness axes: **statement-level** cheats (`: True`, weakened conclusion, smuggled hypotheses,
proxy objects, theorem-as-`def…:Prop`, vacuity) belong to **faithfulness**; **proof-level** cheats
(`sorry`/`axiom` in helpers, `decide`/`native_decide` hiding, `False.elim` on a false goal,
fake/circular proofs) belong to **proof_integrity**.

## Displayed score and verdict (threshold-gated, NOT the average)

The **displayed score** is the weighted mean:

```
score = 0.40·faithfulness + 0.40·proof_integrity + 0.20·code_quality      (0–5)
```

The **verdict** is gated on the individual rubric thresholds, not on that average:

| verdict | condition |
|---|---|
| **clean** | all three pass — faithfulness ≥4 AND proof_integrity ≥3 AND code_quality ≥3 |
| **rejected** | faithfulness ≤2 **OR** proof_integrity ≤2 (a correctness rubric is materially wrong / cheating) |
| **flagged** | everything else (e.g. faithfulness =3, or code_quality ≤2) |

**Style alone never rejects.** `code_quality` can only ever drop a node to *flagged* — it can never
trigger a rejection. (A weak `code_quality` with both correctness rubrics passing ⇒ flagged, not
rejected.) Evaluate the gates in order: check the two reject conditions first; if neither fires and
all three pass, the verdict is clean; otherwise flagged.

## How to grade (per reviewer)

1. Read the **source** statement (named by the node's `source_refs`; the informal statement is in
   `informal_content/<id>.md`) and the **Lean declaration** the node claims (`mathlib_declarations`).
2. Verify foundations independently for the proof_integrity axis: run `#print axioms <decl>` (via
   `lake env lean` or the LSP) and check only `propext`, `Classical.choice`, `Quot.sound` appear;
   flag `sorryAx`.
3. Score the one rubric you were given, 0–5, with a one-paragraph justification grounded in concrete
   evidence (cite the line / lemma), not vibes. Emit strict JSON `{"score", "reasoning"}` (the
   proof_integrity rubric additionally emits `axiom_only` + `axiom_verdicts`).
4. The orchestrator combines the three scores into the displayed score and the verdict above.

## Writing to the sidecar (`review_status.json`)

Each reviewer's score lands in the node's **`ai`** slot, keyed by node `id`. After all three run,
the `ai` slot holds the three integers, the computed `verdict`, and a timestamp:

```jsonc
"reviews": {
  "<node id>": {
    "ai": { "faithfulness": 4, "proof_integrity": 2, "code_quality": 5,
            "verdict": "rejected", "at": "<iso>" }
} }
```

The **`ai` slot is the jury's only write.** Re-running the jury rewrites `ai` only; it never touches
a `human` slot (human verdicts are immutable). Effective verdict = `human` if present, else `ai`.

## The spec-gate

The **faithfulness** rubric, run on the DAG's target/sink nodes, *is* the spec-gate — a faithfulness
check on the project's main results against the source's actual main theorems. Same rubric, same
`autoform-reviewer`, filtered to the roots. No separate machinery.

## Related

Consumes **lean-conventions** (the `code_quality` yardstick) and **formalization-workflow** (the
axiom/`sorry` honesty gates). The three rubrics drive the `ai` slot read by the `review` skill /
review surface. `graph-reviewer` and `holistic-reviewer` review DAG structure — orthogonal to this
node-level jury, not part of it.
