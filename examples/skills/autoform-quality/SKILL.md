---
name: autoform-quality
description: >
  Mathlib code quality inspection — naming, tactic usage, proof structure, and style.
  Does NOT evaluate mathematical correctness (use autoform-review for that).
  Use when checking Lean code style or reviewing for Mathlib conventions compliance.
  Triggers on: /autoform-quality, "style check", "quality check", "mathlib lint".
---

# Code Quality Inspection

Evaluate Lean 4 code for Mathlib conventions and idiomatic style. Focus solely on code quality — do NOT evaluate mathematical correctness or faithfulness to source material.

## Scope

You evaluate **naming, tactic usage, proof structure, and code style** only.

**OUT OF SCOPE** — do not reject or comment on:
- Whether `axiom`, `sorry`, or `unproved` is used (that's the correctness reviewer's domain)
- Whether a proof is complete or incomplete
- Whether a statement matches the source material
- Whether the mathematical content is correct

## Proof Style

- **Search before proving.** Prefer `exact`/`apply`/`rw` with Mathlib lemmas over reproving.
- **`simp only [...]`** with explicit lemma lists for non-terminal simplification.
- **Clear structure over golfing.** No dense one-liners. Use `calc` for chains.
- **`ext`/`funext`** for equality, not unfolding definitions.
- **Prefer API lemmas over `unfold`.** Use `rw`/`simp` with `foo_def`, `foo_apply`.
- **Handle trivial cases early.** Split off degenerate cases at the start.
- **`suffices`** for key intermediate goals, **`have`** for facts, **`let`** for data.
- **Prefer `refine` over `apply`** for visible subgoals with `?_`.

## Naming

- `snake_case` for theorems/lemmas, `UpperCamelCase` for types/classes, `lowerCamelCase` for terms.
- Standard suffixes: `_iff`, `_of_`, `_inj`, `_mono`, `_left`, `_right`, `_eq_`, `_def`, `_apply`.
- **Namespaces must be descriptive mathematical topic names** (e.g., `GroupCohomology`, `GeometryOfNumbers`). Never chapter/section numbers, task IDs, or declaration names.
- Full words — `NormalOrderCoefficients` not `NormalOrderCoeff`.

## Types & Hypotheses

- **Weakest sufficient typeclasses.**
- Remove unused hypotheses.
- Use named arguments `(R := R)` over positional `@foo _ _ _`.
- Prefer `Finite` over `Fintype` when only finiteness is needed.
- Use `by classical` inside proofs, not `Classical` in statements.

## Code Style

- Top-level declarations at column 0. Indent proof bodies by 2 spaces.
- One tactic per line.
- No blank lines inside proofs.
- Dot notation when it improves readability.
- Open namespaces sparingly — prefer `open ... in`.
- Binders before the colon.

## Response Format

If the code quality is acceptable:
```
APPROVED: <brief reason>
```

If the code needs style fixes:
```
REJECTED: <specific, actionable feedback>

Issues found:
1. <specific issue with file path and line numbers>

Suggested fixes:
1. <how to fix>
```
