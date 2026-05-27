# Lean 4 Syntax & Declarations

## Axiom vs Theorem vs Sorry

- `theorem foo := by sorry` — leaves a gap, visible via `#print axioms` as `sorryAx`. Preferred.
- `axiom foo` — creates a permanent unproved constant. Worse than sorry. Reviewers reject.
- Converting sorry → axiom or axiom → sorry without proving anything has zero value.

## Docstrings

- `/-- ... -/` (double dash) is a docstring — MUST immediately precede a declaration.
- `/- ... -/` (single dash) is a floating comment.

## Universe Polymorphism

- Use `universe u` when induction changes the codomain type.
- Declare all type variables in the same universe for type-changing induction.
