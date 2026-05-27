# Integrals, Measures & Measurability

## Integrability

- `Integrable.mono'`: if `g` integrable and `‖f‖ ≤ g` a.e., then `f` integrable. Note: `g : α → ℝ`.
- Schwartz functions are always integrable: `φ.integrable`.
- `norm_setIntegral_le_of_norm_le_const`: `‖∫_S f‖ ≤ C · μ(S)` if `‖f‖ ≤ C` on `S`.

## Switching Sums & Integrals

- `intervalIntegral.integral_finset_sum`: `∫ ∑ f j = ∑ ∫ f j` (requires integrability of each summand).
- `Finset.sum_comm`: swap double sums.

## Measurability

- `Continuous.measurable` works when both spaces have `BorelSpace`.
- `Continuous.aestronglyMeasurable` for a.e. measurability from continuity.
- `measurable_generateFrom`: prove measurability by checking preimages of generators.

## Compact Sets

- `isCompact_closedBall`, `isCompact_sphere` (needs `ProperSpace`).
- `IsCompact.elim_finite_subcover`: extract finite subcover from open cover.
