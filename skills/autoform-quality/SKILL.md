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

<!-- TODO: Add remaining out-of-scope items (source material matching, mathematical correctness). See examples/skills/autoform-quality/SKILL.md for the full version. -->

## Proof Style

- **Search before proving.** Prefer `exact`/`apply`/`rw` with Mathlib lemmas over reproving.
- **`simp only [...]`** with explicit lemma lists for non-terminal simplification.
- **Clear structure over golfing.** No dense one-liners. Use `calc` for chains.

<!-- TODO: Add remaining proof style rules (ext/funext, prefer API lemmas, handle trivial cases, suffices/have/let, refine over apply). See examples/skills/autoform-quality/SKILL.md for the full version. -->

## Naming

- `snake_case` for theorems/lemmas, `UpperCamelCase` for types/classes, `lowerCamelCase` for terms.
- Standard suffixes: `_iff`, `_of_`, `_inj`, `_mono`, `_left`, `_right`, `_eq_`, `_def`, `_apply`.
- **Namespaces must be descriptive mathematical topic names** (e.g., `GroupCohomology`, `GeometryOfNumbers`). Never chapter/section numbers, task IDs, or declaration names.

<!-- TODO: Add remaining naming rules (full words, no abbreviations). See examples/skills/autoform-quality/SKILL.md for the full version. -->

## Types & Hypotheses

- **Weakest sufficient typeclasses.**
- Remove unused hypotheses.
- Use named arguments `(R := R)` over positional `@foo _ _ _`.

<!-- TODO: Add remaining type/hypothesis rules (Finite over Fintype, by classical). See examples/skills/autoform-quality/SKILL.md for the full version. -->

## Code Style

- Top-level declarations at column 0. Indent proof bodies by 2 spaces.
- One tactic per line.
- No blank lines inside proofs.

<!-- TODO: Add remaining code style rules (dot notation, open namespaces, binders before colon). See examples/skills/autoform-quality/SKILL.md for the full version. -->

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
