# Type coercions & casting

## Nat ↔ Real powers

- `Real.rpow_natCast (x : ℝ) (n : ℕ) : x ^ (↑n : ℝ) = x ^ n`.
- `Nat.cast_sub {m n : ℕ} (h : m ≤ n) : (↑(n - m) : ℝ) = ↑n - ↑m` — note the `m ≤ n` side
  condition (`Nat` subtraction truncates).
- Use `norm_cast` or `push_cast` to normalize coercions, e.g. after `interval_cases`.

## `push_cast` / `norm_cast` / `mod_cast`

- `push_cast` pushes coercions toward the leaves (`↑(a - b)` → `↑a - ↑b`), so ring/linear tactics
  can see the structure. Run it before `ring`/`linarith` on a mixed-cast goal.
- `norm_cast` / `mod_cast` move casts out of the way to discharge a goal that is "the same up to
  coercion."

## `WithTop ℕ∞` ambiguity (ContDiff)

- `ContDiff 𝕜 ⊤ f` — bare `⊤ : WithTop ℕ∞` means analytic (strongest).
- `ContDiff 𝕜 (↑⊤ : ℕ∞) f` — the coercion of `ℕ∞`'s top means C^∞ (smooth).
- APIs that require `↑⊤` rather than `⊤` are fixed with `.of_le le_top`.

## NNReal vs ℝ

- Construct an `ℝ≥0` via `⟨myVal, proof⟩`.
- Normalize the coercion back to `ℝ` with `simp only [NNReal.coe_mk]`.

## Scalar-field ambiguity

- When two scalar instances coexist (e.g. `NormedSpace ℝ ℂ` and `NormedSpace ℂ ℂ`), always
  specify the field with a named argument: `iteratedFDeriv (𝕜 := ℝ) …`. Pinning the field also
  speeds up instance search.
