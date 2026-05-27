# Norms, Inner Products & Bounds

## Norm Bounds

- `norm_mul_le a b : ‖a * b‖ ≤ ‖a‖ * ‖b‖`.
- `norm_sub_norm_le a b : ‖a‖ - ‖b‖ ≤ ‖a - b‖` (one-sided).
- `abs_norm_sub_norm_le a b : |‖a‖ - ‖b‖| ≤ ‖a - b‖` (two-sided).
- For lower bounds: `linarith` with `norm_sub_norm_le`.

## Inner Products (Mathlib Convention)

- **Conjugate-linear in first arg, linear in second** (physics convention).
- `inner_conj_symm`, `inner_add_left`, `inner_smul_left`, `inner_smul_right`.
- `inner_self_eq_norm_sq : ‖v‖² = re⟨v,v⟩`.

## Finite Sums & Products

- `Finset.single_le_sum`: requires explicit `(f := ...)` when Lean can't infer.
- `Finset.prod_pow_eq_pow_sum`: `∏ a^(f i) = a^(∑ f i)`.
- `Finset.sum_subset`: extend sum to larger set if extra terms are zero.

## Infinite Sums

- `tsum_le_tsum` for pointwise comparison.
- `Summable.sum_add_tsum_nat_add n`: split tsum into finite prefix + tail.
