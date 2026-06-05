# Type Coercions & Casting

## Nat ↔ Real Powers

- `Real.rpow_natCast (x : ℝ) (n : ℕ) : x ^ (↑n : ℝ) = x ^ n`
- `Nat.cast_sub {m n : ℕ} (h : m ≤ n) : (↑(n - m) : ℝ) = ↑n - ↑m`
- Use `norm_cast` or `push_cast` to normalize after `interval_cases`.

## WithTop ℕ∞ Ambiguity (ContDiff)

- `ContDiff 𝕜 ⊤ f` — bare `⊤ : WithTop ℕ∞` means analytic (strongest).
- `ContDiff 𝕜 (↑⊤ : ℕ∞) f` — coercion of `ℕ∞`'s top means C^∞ (smooth).
- SchwartzMap APIs require `↑⊤`, not `⊤`. Fix with `.of_le le_top`.

## NNReal vs ℝ

- Construct via `⟨myVal, proof⟩ : ℝ≥0`.
- Normalize coercions with `simp only [NNReal.coe_mk]`.

## Scalar Field Ambiguity

- When both `NormedSpace ℝ ℂ` and `NormedSpace ℂ ℂ` exist, always specify: `iteratedFDeriv (𝕜 := ℝ)`, `SchwartzMap.seminorm ℂ k n`.
