# Tactic patterns & pitfalls

## Simp

- `simp` can fail inside `conv` blocks. Use `show`, `change`, or `rw` instead.
- `unfold` also fails in `conv`. Use equalities or `rw [show ... from ...]`.
- Use `unfold`/`rfl` instead of `simp` for purely definitional unfoldings (faster, more
  predictable).
- Non-terminal `simp` should be `simp only [...]` with an explicit lemma list; reserve bare
  `simp` for the goal-closing step.

## Rewrite ordering

- Order matters: `rw [norm_mul, mul_pow, ...]` — power distribution must come BEFORE simplifying
  the norm factors.
- After `interval_cases`, normalize casts: `simp only [Nat.cast_zero, Nat.cast_one]` or
  `norm_cast`.

## Ring

- `ring` does NOT work on `rpow` expressions. It only works after `congr 1` exposes the real
  exponent.
- `ring` does NOT work when `•` (smul) is mixed with `*` (mul). Use `smul_eq_mul` first.

## Conv blocks

- `conv_lhs => rw [← h]` or nested `conv` blocks can cause heartbeat timeouts in large files.
  Prefer a direct `rw` when possible.

## Positivity

- For norm non-negativity, reach for `positivity`; when it can't see the structure, supply the
  explicit lemma (e.g. an `…_nonneg` lemma for the object in hand).
