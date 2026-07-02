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

The grading criteria the AI jury applies to a formalized node. The jury is **blind
single-axis reviewers**, each given ONLY its own rubric — never the others'. Each rubric is an
integer score 0–5 with a pass threshold; the criteria and prompt templates are the JSON files in
`references/` (from a subagent, read them at
`${CLAUDE_PLUGIN_ROOT}/skills/eval-rubrics/references/<axis>.json` — subagents run with cwd set to
the user's project, so plugin-relative paths do not resolve).

## The jury rubrics

| Rubric | Weight | Pass ≥ | Reviewer agent | Judges whether… |
|---|---|---|---|---|
| **faithfulness** | 0.40 | 4/5 | `faithfulness-reviewer` | the Lean **statement** captures the source statement *at full strength* (no weakening, no vacuity) |
| **proof_integrity** | 0.40 | 3/5 | `proof-integrity-reviewer` | the **proof** chain is genuine work on sound foundations (axioms clean, no disguised `sorry`/cheats) |
| **code_quality** | 0.20 | 3/5 | `code-quality-reviewer` | the code follows Mathlib conventions and idiomatic Lean 4 (yardstick = the **autoform** skill) |

The jury is **whatever rubric files live in `references/`** — currently these three. The
cheating-hunt splits along the two correctness axes: **statement-level** cheats (`: True`, weakened
conclusion, smuggled hypotheses, proxy objects, theorem-as-`def…:Prop`, vacuity) belong to
**faithfulness**; **proof-level** cheats (`sorry`/`axiom` in helpers, `decide`/`native_decide`
hiding, `False.elim` on a false goal, fake/circular proofs) belong to **proof_integrity**.

## Modular by design — the files ARE the jury

The rubric files in `references/` are the **single source of truth**: the dashboard, the
deterministic dispatcher, and the verdict gate all read the axis set, weights, thresholds and
gating roles from them at load time (`review_model.rubric_specs()`), so the reviewer system is
changed by editing JSON, never code:

- **Add an axis** — drop a new `<axis>.json` (`name`, `weight`, `pass_threshold`, `reviewer`,
  `criteria`, `prompt_template`) plus its judge agent; the jury, the parallel fan-out, and the
  weighted score pick it up automatically.
- **Shrink to a single reviewer** — leave one rubric file; the verdict gate works with one axis.
- **Tune the gate per axis, in data** — `reject_at_or_below` sets the score that forces *rejected*
  (correctness axes); `verdict_ceiling: "flagged"` marks a *style* axis that can never reject.

Because everything derives from the files, **dropping a `reviewer` on a node always runs the current
rubric set** — changing the jury never breaks the dispatch.

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
4. The deterministic dispatcher parses each reviewer's JSON, persists the scores, and combines the
   per-axis scores into the displayed score and the verdict above.

**Abstaining.** When a dispatch is missing a required input (e.g. no ground-truth source named for
a correctness axis), the reviewer does not score against its own reconstruction — it returns
`{"score": null, "error": "<what was missing>"}`. A `null` score is an explicit abstain: the
dispatcher records no score for that axis and derives no verdict from it, so a dispatch bug is
distinguishable from a genuine 0.

## Writing to the sidecar (`review_status.json`)

The reviewers never write the sidecar themselves — the deterministic dispatcher parses each
reviewer's JSON output and persists it. Each reviewer's score lands in the node's **`ai`** slot,
keyed by node `id`. After all three run, the `ai` slot holds the three integers, the computed
`verdict`, and a timestamp:

```jsonc
"reviews": {
  "<node id>": {
    "ai": { "faithfulness": 4, "proof_integrity": 2, "code_quality": 5,
            "verdict": "rejected", "at": "<iso>" }
} }
```

The **`ai` slot is the only slot jury scores ever touch.** Re-running the jury rewrites `ai` only;
it never touches a `human` slot (human verdicts are immutable). Effective verdict = `human` if
present, else `ai`.

## The spec-gate

The **faithfulness** rubric, run on the DAG's target/sink nodes, *is* the spec-gate — a faithfulness
check on the project's main results against the source's actual main theorems. Same rubric, same
`faithfulness-reviewer`, filtered to the roots. No separate machinery.

## Related

Consumes **autoform** (the `code_quality` yardstick) and **autoform-prove** (the
axiom/`sorry` honesty gates). The three rubrics drive the `ai` slot read by the `review` skill /
review surface. `graph-reviewer` and `holistic-reviewer` review DAG structure — orthogonal to this
node-level jury, not part of it.
