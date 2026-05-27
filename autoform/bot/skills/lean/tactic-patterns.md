# Tactic Patterns & Pitfalls

## Simp

- `simp` can fail inside `conv` blocks. Use `show`, `change`, or `rw` instead.
- `unfold` also fails in `conv`. Use equalities or `rw [show ... from ...]`.
- Use `unfold`/`rfl` instead of `simp` for purely definitional unfoldings (faster, more predictable).

## Rewrite Ordering

- Order matters: `rw [norm_mul, mul_pow, ...]` — power distribution must come BEFORE simplifying norm factors.
- After `interval_cases`, normalize casts: `simp only [Nat.cast_zero, Nat.cast_one]` or `norm_cast`.

## Ring

- `ring` does NOT work on `rpow` expressions. Only works after `congr 1` exposes the real exponent.
- `ring` does NOT work when `•` (smul) is mixed with `*` (mul). Use `smul_eq_mul` first.

## Conv Blocks

- `conv_lhs => rw [← h]` or nested `conv` blocks can cause heartbeat timeouts in large files. Prefer direct `rw` when possible.

## Positivity

- For norm non-negativity of `iteratedFDeriv`: use `positivity` or `(iteratedFDeriv ℝ n f x).opNorm_nonneg` explicitly.
