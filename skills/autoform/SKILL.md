---
name: autoform
description: >
  Mathlib & Lean 4 conventions for writing formalization-quality code.
  Distilled from 94k PR review comments and 165k Zulip messages.
  Use when writing Lean 4 code, formalizing mathematics, or working with Mathlib.
  Triggers on: /autoform, "lean conventions", "mathlib style", "formalize".
---

# Mathlib & Lean 4 Conventions

Key conventions for writing Mathlib-compatible Lean 4 code, distilled from 792 community conventions extracted from ~94k GitHub PR review comments and ~165k Zulip messages.

## Proof Style

- **Search before proving.** Use `exact?`, `apply?`, `rw?` to find existing Mathlib lemmas. Prefer `exact`/`apply`/`rw` over reproving known facts.
- **`simp only [...]`** with explicit lemma lists for non-terminal simplification. Plain `simp` is fine when it closes the goal.
- **Clear structure over golfing.** No dense one-liners. Use `calc` for chained equalities/inequalities.

<!-- TODO: Add remaining proof style rules (ext/funext, factor helpers, prefer API lemmas, handle trivial cases, suffices/have/let, refine over apply). See examples/skills/autoform/SKILL.md for the full version. -->

## Naming

- `snake_case` for theorems/lemmas, `UpperCamelCase` for types/classes, `lowerCamelCase` for terms.
- Standard suffixes: `_iff`, `_of_`, `_inj`, `_mono`, `_left`, `_right`, `_eq_`, `_def`, `_apply`.
- **Namespaces = mathematical topics** (e.g., `GroupCohomology`, `GeometryOfNumbers`). Never chapter/section numbers.

<!-- TODO: Add remaining naming rules (one concept one name, standard mathematical terminology). See examples/skills/autoform/SKILL.md for the full version. -->

## Types & Hypotheses

- **Weakest sufficient typeclasses** (`Semiring` over `Ring`, `Preorder` over `LinearOrder`).
- Remove unused hypotheses. Implicit for inferable args, explicit otherwise.
- Use named arguments `(R := R)` over positional `@foo _ _ _` when specifying implicits.

<!-- TODO: Add remaining type/hypothesis rules (Finite over Fintype, by classical). See examples/skills/autoform/SKILL.md for the full version. -->

## Key Tactics

| Goal shape | Tactic | Notes |
|---|---|---|
| `0 < x`, `0 ≤ x` | `positivity` | |
| Nat/Int arithmetic | `omega` | |
| Concrete numerics | `norm_num` | |

<!-- TODO: Add remaining tactic table rows (gcongr, ring/field_simp, linarith/nlinarith, field_simp, push_cast, norm_cast, calc, simp_rw/conv, split_ifs, decide). See examples/skills/autoform/SKILL.md for the full version. -->

## Simp Conventions

- **Terminal `simp`** (closing the goal) is fine with broad lemma sets.
- **Non-terminal `simp`** must use `simp only [...]` with explicit lemmas.
- **`@[simp]`** only for genuinely useful canonical lemmas (evaluation, coercion, projection, constructor). Avoid lemmas with hard side conditions.

<!-- TODO: Add remaining simp rules (orient simp lemmas, @[simps], simp? discovery). See examples/skills/autoform/SKILL.md for the full version. -->

## API Design

- **Reuse existing Mathlib abstractions.** Don't redefine what exists.
- Use canonical constructors (`Subtype.mk`, `.val`, `Equiv.ofBijective`).
- After new definitions, provide `_def`/`_apply` lemmas and basic `@[simp]` lemmas.

<!-- TODO: Add remaining API design rules (@[ext] lemmas, high-level APIs over element-chasing). See examples/skills/autoform/SKILL.md for the full version. -->

## Code Style

- Top-level declarations at column 0. Indent proof bodies by 2 spaces.
- One tactic per line (unless a short one-liner proof).
- No blank lines inside proofs.

<!-- TODO: Add remaining code style rules (dot notation, parentheses, open namespaces, binders before colon). See examples/skills/autoform/SKILL.md for the full version. -->

## Common Pitfalls

- **`Real.log 0 = 0`** in Mathlib (not undefined). Same: `0⁻¹ = 0`, `0 / 0 = 0`.
- **`Nat` subtraction truncates**: `5 - 7 = 0`. Use `Int` for negative results.
- **`Nat.cast_sub` requires `h : b ≤ a`** — provide the proof or work in `ℤ`.

<!-- TODO: Add remaining pitfalls (rpow vs pow, Finset.card_fin, div_le_iff₀, push_cast, beta redexes, Function.update_same, norm_num on transcendentals, field_simp on sums, erw). See examples/skills/autoform/SKILL.md for the full version. -->

## Lean 4 Syntax Quick Reference

- `theorem foo := by sorry` — gap visible via `#print axioms` as `sorryAx`.
- `axiom foo` — permanent unproved constant. Worse than sorry.
- `/-- ... -/` (double dash) is a docstring — MUST immediately precede a declaration.

<!-- TODO: Add remaining syntax reference items (single-dash comments, universe u). See examples/skills/autoform/SKILL.md for the full version. -->

## Tactic Patterns & Pitfalls

- `simp` can fail inside `conv` blocks — use `show`, `change`, or `rw` instead.
- `unfold` also fails in `conv` — use equalities or `rw [show ... from ...]`.
- Rewrite ordering matters: `rw [norm_mul, mul_pow, ...]` — distribute before simplifying.

<!-- TODO: Add remaining tactic patterns (ring vs rpow, ring vs smul, conv heartbeat timeouts, norm non-negativity). See examples/skills/autoform/SKILL.md for the full version. -->

## Existential Witnesses & Choice

- When axiomatizing `∃ x, P x`, axiomatize `P c` for the specific witness `c`.
- `choose` with guards creates dependent functions (can't use with `Finset.sup`). Fix: drop the guard.

<!-- TODO: Add remaining existential/choice rules (propositional proof equality in rewrites). See examples/skills/autoform/SKILL.md for the full version. -->
