# Existence, Choice & Proof Patterns

## Existential Witnesses

- When axiomatizing `∃ x, P x`, axiomatize `P c` for the specific witness `c` from the textbook.
- Then prove existential by `⟨c, axiom⟩`. This provides mathematical content (the witness).

## Choose Pitfalls

- `choose` with guards `(j : ℕ) → j < K → ∃ N, P j N` creates dependent functions (can't use with `Finset.sup`).
- Fix: drop the guard when possible — `(j : ℕ) → ∃ N, P j N` gives `Nf : ℕ → ℕ` (simple).

## Proof Irrelevance

- Any two proofs of the same proposition are definitionally equal in rewrites.

## Vacuous Hypotheses

- If a hypothesis is always true (e.g., `ω ∉ f '' S` when norms are incompatible), the axiom can be simplified away.

## Uniform Limits

- For sequences: build uniform limits of `iteratedFDeriv ℝ m f_n`, then use `hasFDerivAt_of_tendstoUniformlyOn`.
- `contDiff_tsum` for smoothness of infinite sums — interchanges with derivatives.
