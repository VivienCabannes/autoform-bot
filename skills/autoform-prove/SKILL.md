---
name: autoform-prove
description: >
  Lean 4 proof strategies and workflow. Incremental proving, REPL prototyping,
  sorry/axiom handling, false statement detection, and the unproved macro policy.
  Use when proving theorems, debugging proofs, or handling sorry/axiom in Lean 4.
  Triggers on: /autoform-prove, "prove this", "sorry handling", "proof strategy".
---

# Proof Strategies & Workflow

How to approach Lean 4 formalization: from reading source material through completing proofs.

## Core Workflow

1. **Search first** — search Mathlib using `exact?`, `apply?`, `rw?`, `lean_loogle`, `mathlib_grep`, `mathlib_find_name` before writing proofs from scratch. Many standard results already exist.
2. **Prototype in REPL** — use `run_lean_code` to test proof fragments before editing large files. Large files take 120+ seconds to build; testing in isolation saves time.
3. **Work incrementally** — fix compilation errors first, then triage sorrys by difficulty (easy → medium → hard). Prove easy theorems first and commit each one.
4. **Commit often** — each proved theorem gets its own commit. Name commits after the task: `convex-sets-def: formalize convex set definitions`.

## Sorry Handling

The minimum bar for acceptance is net sorry reduction.

**Rules:**
- Never decompose a single sorry into multiple sorry'd helpers — reviewers reject if sorry count increases.
- Never redistribute sorry by creating sorry'd helpers and proving the target from them — `lean_verify` detects `sorryAx` in the axiom list.
- Never introduce a new sorry to close an existing one. If changing a definition breaks other proofs, fix ALL of them.
- If you can't prove a helper, inline the attempt instead of leaving it as a separate sorry'd lemma.
- Leaving a sorry as-is is always better than shuffling it around.

## Axiom Policy

Never use the `axiom` keyword to replace `sorry` — it is a cheating pattern.

- If an axiom exists in the code, convert it to `theorem ... := by sorry` preserving the exact signature.
- When decomposing, split into genuinely distinct sub-results, not weaker versions of the main theorem.
- Axiom-to-sorry conversion alone is not progress — you must also attempt the proof.
- Never shuffle axioms (rename, split, or recombine without proving anything).

## The `unproved` Macro

For statements whose proof is not given in the source material:

```lean
unproved theoremName (args : Types) : Conclusion
```

This compiles to `@[unproved] axiom theoremName ...` and marks the declaration as a justified gap.

**When to use `unproved`:**
- The book says "proof omitted" or "left as exercise"
- The book references another source instead of a proof
- The book states a result without any proof

**When NOT to use `unproved`:**
- The book provides a proof (even a sketch) — you must prove it
- You find the proof too difficult — keep trying
- A Mathlib lemma is needed but missing — prove it yourself

**`sorry` and raw `axiom` are never acceptable** as final state. `sorry` introduces `sorryAx` which breaks soundness downstream. Raw `axiom` without `@[unproved]` is equally penalized.

## Detecting False Statements

Some formalized statements are mathematically false. Detect early:

- Try small parameter instantiations (n=1, zero function, degenerate cases).
- If three independent proof paths fail at the same point, investigate whether the statement is false.
- Watch for type-space confusion where the same Lean type represents two different mathematical spaces.

**Handling:** Document the counterexample in a comment, report the issue, and leave as sorry. Do NOT replace sorry with axiom or shuffle to helpers.

## Anti-Cheating Checklist

These patterns are always rejected:

1. **Trivial substitution** — replacing a theorem's statement with `True` while keeping name/docstring.
2. **Encoding theorems as definitions** — `def foo (...) : Prop := <statement>` for something the book proves.
3. **Smuggling assumptions** — structure fields that include what should be proved as theorems.
4. **Weakening content** — proving a weaker numerical shadow instead of the actual result.
5. **Modeling avoidance** — replacing mathematical objects with simpler algebraic proxies without proving faithfulness.
6. **Unacknowledged sorry/axiom** — sorry in helper lemmas called by "proved" theorems. Always grep the entire project.

## When Stuck

Every task is achievable given enough time. If stuck:
1. Search Mathlib using `lean_loogle` or `mathlib_grep` for relevant lemma names
2. Break the proof into smaller `have` steps that mirror the informal argument
3. Try `exact?`, `apply?`, `simp?` to discover the right tactics
4. Read error messages carefully — they tell you exactly what Lean needs
5. Restructure your approach rather than escalating
