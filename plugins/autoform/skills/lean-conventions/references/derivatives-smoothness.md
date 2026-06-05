# Derivatives & Smoothness

## ContDiff Variations

- Avoid `contDiff_succ_iff_fderiv` (requires `AnalyticOnNhd` for `n = ⊤`).
- Use `contDiff_of_differentiable_iteratedFDeriv` — works for `n : ℕ∞` including `⊤`.
- Use `contDiff_infty_iff_fderiv` for C^∞ specifically.

## Smooth Operations

- `ContDiff.add`, `.sub`, `.mul`, `.comp` chain via dot notation.
- `contDiff_const_smul s : ContDiff ℝ ⊤ (fun x => s • x)`.
- `ContDiffOn.div` requires denominator nonzero on the entire set.

## Leibniz Rule (Iterated Derivatives)

- `norm_iteratedFDeriv_mul_le hf hg x` gives: `‖D^n(f·g) x‖ ≤ ∑ binom(n,i) · ‖D^i f x‖ · ‖D^(n-i) g x‖`.
- Both `f`, `g` must be `ContDiff 𝕜 N` with `n ≤ N`. Codomain must be `NormedRing`.

## lineDeriv vs Fderiv

- `lineDeriv ℝ f x v = fderiv ℝ f x v` (via `DifferentiableAt.lineDeriv_eq_fderiv`).
- `lineDeriv` of a non-differentiable function returns 0.
