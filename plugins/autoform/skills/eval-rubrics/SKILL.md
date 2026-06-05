---
name: eval-rubrics
description: >-
  Use when grading, scoring, or reviewing a Lean 4 formalization against a source text — judging
  whether a Lean statement faithfully captures a book statement, whether a proof is genuine,
  whether code follows Mathlib style, or producing a rubric scorecard. Provides the
  autoform-bot jury rubrics (faithfulness, proof_integrity, code_quality, correctness, style)
  with weights and pass thresholds, plus a complementary 7-dimension auto-rater. Use for any
  "evaluate / rate / is this formalization good" request on Lean code.
---

# Evaluation rubrics for formalized mathematics

The grading criteria a jury of judges applies to a formalized declaration. Each rubric is a
score 0–5 with a pass threshold; the overall verdict is a weighted aggregate. Full criteria and
prompt templates are the JSON files in `references/` (the autoform-bot rubric definitions); a
complementary 7-dimension rater is `references/seven-dimension-rater.md`.

## Default jury (active rubrics)

| Rubric | Weight | Pass ≥ | Judges whether… |
|---|---|---|---|
| **faithfulness** | 0.40 | 4/5 | the Lean statement captures the book statement *at full strength* (no weakening, no vacuity) |
| **proof_integrity** | 0.40 | 3/5 | the proof chain is genuine work on sound foundations (axioms clean, no disguised `sorry`) |
| **code_quality** | 0.20 | 3/5 | the code follows Mathlib conventions and idiomatic Lean 4 |

`correctness` (0.40, ≥3) and `style` (0.20, ≥3) are also active and can stand in for
faithfulness/code_quality in lighter passes. `alignment` and `formatting` ship inactive
(`active: false`) — enable by flipping the flag in the JSON.

## How to grade (mirrors the autoform-bot judge agent)

1. Read the **source** statement (book / LaTeX) and the **Lean declaration** it claims to be.
2. Verify foundations independently: run `#print axioms <decl>` (via `lake env lean`) and check
   only `propext`, `Classical.choice`, `Quot.sound` appear; flag `sorryAx`.
3. Score each active rubric 0–5 with a one-paragraph justification grounded in concrete evidence
   (cite the line / lemma), not vibes.
4. Aggregate: weighted mean; a target **passes** only if every active rubric clears its
   threshold.
5. Emit a scorecard: per-rubric score + reason, axioms found, overall pass/fail, and actionable
   fixes.

## Seven-dimension rater

`references/seven-dimension-rater.md` scores 1–5 across quality, math_correctness, generality,
api_coverage, concision, modern_lean4, structural_focus — a quick single-line post-extraction
diagnosis. Use it for a fast read; use the jury rubrics above for a gating verdict.

## Related

Consumes **lean-conventions** (the code_quality/style yardstick) and **formalization-workflow**
(the axiom/sorry honesty gates). Invoked by `autoform:eval`.
