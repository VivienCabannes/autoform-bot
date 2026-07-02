# Existence, choice & proof patterns

## Existential witnesses

- When axiomatizing `∃ x, P x`, axiomatize `P c` for the specific witness `c` from the source.
- Then prove the existential by `⟨c, axiom⟩`. This carries mathematical content (the witness),
  rather than asserting bare existence.
- **Scope:** this pattern applies only within the sanctioned-placeholder / audited-ledger
  regime, where the axiom is tracked and satisfiability-vetted (see the **autoform-prove**
  axiom policy) — never as a way to dodge a proof.

## `choose` pitfalls

- `choose` with guards `(j : ℕ) → j < K → ∃ N, P j N` creates dependent functions (you can't use
  the result with `Finset.sup`).
- Fix: drop the guard when possible — `(j : ℕ) → ∃ N, P j N` gives `Nf : ℕ → ℕ` (simple,
  non-dependent).

## Proof irrelevance

- Any two proofs of the same proposition are definitionally equal in rewrites — you never need to
  match a specific proof term.

## Vacuous hypotheses

- If a hypothesis is always true (e.g. an emptiness condition that the types force), the
  statement can often be simplified — but check this is genuine, not a vacuity that makes the
  whole statement trivial (a vacuous statement proves nothing; see the **autoform-prove**
  false-statements reference).

## Uniform limits & smoothness (pointers)

- For sequences, build the uniform limit first, then transfer the derivative with the appropriate
  `hasFDerivAt_of_tendstoUniformlyOn`-style lemma.
- For smoothness of an infinite sum, `contDiff_tsum` interchanges differentiation with the sum.
  (Deeper analysis API lives in the deferred analysis guides.)
