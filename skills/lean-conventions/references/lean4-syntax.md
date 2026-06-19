# Lean 4 syntax & declarations

## Axiom vs theorem vs sorry

- `theorem foo := by sorry` — leaves a gap, visible via `#print axioms` as `sorryAx`. Preferred
  over an `axiom` during development.
- `axiom foo` — creates a permanent unproved constant. Worse than `sorry`; reviewers reject it
  as a finished proof.
- Converting `sorry → axiom` or `axiom → sorry` without proving anything has zero value (see the
  **formalization-workflow** axiom policy).

## Docstrings

- `/-- ... -/` (double dash) is a docstring — it MUST immediately precede a declaration.
- `/- ... -/` (single dash) is a floating comment.

## Universe polymorphism

- Use `universe u` when induction changes the codomain type.
- Declare all type variables in the same universe for type-changing induction.

## Binders & statements

- Put binders before the colon: `lemma foo (x : α) (h : P x) : Q x`, not a trailing `∀ x, …`.
- Mark inferable arguments implicit `{x : α}`, the rest explicit.
- Keep `Classical` off the statement — reach for `by classical` inside the proof when you need
  choice/decidability.
