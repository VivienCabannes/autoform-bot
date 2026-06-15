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

<!-- TODO: Add remaining workflow step (commit often, naming convention for commits). See examples/skills/autoform-prove/SKILL.md for the full version. -->

## Sorry Handling

The minimum bar for acceptance is net sorry reduction.

**Rules:**
- Never decompose a single sorry into multiple sorry'd helpers — reviewers reject if sorry count increases.
- Never redistribute sorry by creating sorry'd helpers and proving the target from them — `lean_verify` detects `sorryAx` in the axiom list.
- Never introduce a new sorry to close an existing one. If changing a definition breaks other proofs, fix ALL of them.

<!-- TODO: Add remaining sorry handling rules (inline failed helpers, leaving sorry as-is is better than shuffling). See examples/skills/autoform-prove/SKILL.md for the full version. -->

## Axiom Policy

Never use the `axiom` keyword to replace `sorry` — it is a cheating pattern.

- If an axiom exists in the code, convert it to `theorem ... := by sorry` preserving the exact signature.
- When decomposing, split into genuinely distinct sub-results, not weaker versions of the main theorem.

<!-- TODO: Add remaining axiom policy rules (axiom-to-sorry alone is not progress, never shuffle axioms). See examples/skills/autoform-prove/SKILL.md for the full version. -->

## The `unproved` Macro

For statements whose proof is not given in the source material:

```lean
unproved theoremName (args : Types) : Conclusion
```

This compiles to `@[unproved] axiom theoremName ...` and marks the declaration as a justified gap.

**When to use `unproved`:**
- The book says "proof omitted" or "left as exercise"
- The book references another source instead of a proof

<!-- TODO: Add remaining "when to use" and "when NOT to use" cases, and the final policy on sorry vs raw axiom. See examples/skills/autoform-prove/SKILL.md for the full version. -->

## Detecting False Statements

Some formalized statements are mathematically false. Detect early:

- Try small parameter instantiations (n=1, zero function, degenerate cases).
- If three independent proof paths fail at the same point, investigate whether the statement is false.

<!-- TODO: Add remaining detection guidance (type-space confusion, handling procedure: document counterexample, report, leave as sorry). See examples/skills/autoform-prove/SKILL.md for the full version. -->

## Anti-Cheating Checklist

These patterns are always rejected:

1. **Trivial substitution** — replacing a theorem's statement with `True` while keeping name/docstring.
2. **Encoding theorems as definitions** — `def foo (...) : Prop := <statement>` for something the book proves.
3. **Smuggling assumptions** — structure fields that include what should be proved as theorems.

<!-- TODO: Add remaining anti-cheating patterns (weakening content, modeling avoidance, unacknowledged sorry/axiom). See examples/skills/autoform-prove/SKILL.md for the full version. -->

## When Stuck

Every task is achievable given enough time. If stuck:
1. Search Mathlib using `lean_loogle` or `mathlib_grep` for relevant lemma names
2. Break the proof into smaller `have` steps that mirror the informal argument

<!-- TODO: Add remaining "when stuck" strategies (exact?/apply?/simp?, read error messages, restructure approach). See examples/skills/autoform-prove/SKILL.md for the full version. -->
