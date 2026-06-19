# Mathlib & Lean 4 conventions

Key conventions for writing Mathlib-compatible Lean 4 code, distilled from 792 community
conventions extracted from ~94k GitHub PR review comments and ~165k Zulip messages.

## Proof style

- **Search before proving.** Use `exact?`, `apply?`, `rw?` to find existing Mathlib lemmas.
  Prefer `exact`/`apply`/`rw` over reproving known facts.
- **`simp only [...]`** with explicit lemma lists for non-terminal simplification. Plain `simp`
  is fine when it closes the goal.
- **Clear structure over golfing.** No dense one-liners. Use `calc` for chained
  equalities/inequalities.
- **`ext`/`funext`** for function/structure equality, not unfolding definitions.
- **Factor repeated arguments** into `private` helper lemmas.
- **Prefer API lemmas over `unfold`.** Use `rw`/`simp` with named lemmas (`foo_def`, `foo_apply`)
  instead of broad `unfold`.
- **Handle trivial cases early.** Split off `x = 0`, `n = 0`, `s = ∅` at the start so the main
  proof stays clean.
- **`suffices`** to expose key intermediate goals. **`have`** for facts, **`let`** for data,
  **`letI`/`haveI`** for local instances.
- **Prefer `refine` over `apply`** when you want visible subgoals with `?_` placeholders.

## Naming

- `snake_case` for theorems/lemmas, `UpperCamelCase` for types/classes, `lowerCamelCase` for
  terms.
- Standard suffixes: `_iff`, `_of_`, `_inj`, `_mono`, `_left`, `_right`, `_eq_`, `_def`,
  `_apply`.
- One concept, one name. Check existing Mathlib names before inventing new ones.
- Prefer standard mathematical and Mathlib terminology over ad hoc names.
- A namespace names a *mathematical topic*, never a task, declaration, or chapter.

## Types & hypotheses

- **Weakest sufficient typeclasses** (`Semiring` over `Ring`, `Preorder` over `LinearOrder`).
- Remove unused hypotheses. Implicit for inferable args, explicit otherwise.
- Use named arguments `(R := R)` over positional `@foo _ _ _` when specifying implicits.
- Prefer `Finite` over `Fintype` in statements when only finiteness is needed.
- Use `by classical` inside proofs rather than adding `Classical` to theorem statements.

## Key tactics

| Goal shape | Tactic | Notes |
|---|---|---|
| `0 < x`, `0 ≤ x` | `positivity` | |
| Nat/Int arithmetic | `omega` | |
| Concrete numerics | `norm_num` | |
| Monotonicity `f a ≤ f b` | `gcongr` | Tag supporting lemmas `@[gcongr]` |
| Ring/field equalities | `ring` / `field_simp` then `ring` | |
| Linear arithmetic | `linarith` / `nlinarith` | |
| Clear denominators | `field_simp` then `ring`/`linarith` | Avoid on sums — can explode |
| Normalize coercions | `push_cast` then arithmetic | |
| Mixed-type casts | `norm_cast` / `mod_cast` | |
| Multi-step chains | `calc` blocks | Clearer than chained rewrites |
| Rewrite under binders | `simp_rw` or `conv` | |
| Split on `if` | `split_ifs with h` | |
| Large finite decision | `decide` or `by decide` | Never `native_decide` in Mathlib |

## Simp conventions

- **Terminal `simp`** (closing the goal) is fine with broad lemma sets.
- **Non-terminal `simp`** must use `simp only [...]` with explicit lemmas.
- **`@[simp]`** only for genuinely useful canonical lemmas (evaluation, coercion, projection,
  constructor). Avoid lemmas with hard side conditions.
- Orient simp lemmas: complicated expression on the left, simpler normal form on the right.
- Use `@[simps]` to auto-generate projection lemmas for structures and equivalences.
- Use `simp?` to discover good lemma lists, then replace with explicit `simp only [...]`.

## API design

- **Reuse existing Mathlib abstractions.** Don't redefine what exists.
- Use canonical constructors (`Subtype.mk`, `.val`, `Equiv.ofBijective`).
- After new definitions, provide `_def`/`_apply` lemmas and basic `@[simp]` lemmas.
- Add `@[ext]` lemmas for structures with natural extensionality.
- Prefer high-level APIs (universal properties, morphism lemmas) over element-chasing.
- Keep one canonical concept per name. Derive variants as corollaries.

## Code style

- Top-level declarations at column 0. Indent proof bodies by 2 spaces.
- One tactic per line (unless a short one-liner proof).
- No blank lines inside proofs.
- Use dot notation (`h.symm`, `f.comp g`) when it improves readability.
- Remove unnecessary parentheses, but add them when precedence is unclear.
- Open namespaces sparingly. Prefer `open ... in` for narrow scope.
- Put binders before the colon: `lemma foo (x : α) (h : P x) : Q x` not trailing `∀`.
- 100-character line width.

## Common pitfalls

- **`Real.log 0 = 0`** in Mathlib (not undefined). Same: `0⁻¹ = 0`, `0 / 0 = 0`.
- **`Nat` subtraction truncates**: `5 - 7 = 0`. Use `Int` for negative results.
- **`Nat.cast_sub` requires `h : b ≤ a`** — provide the proof or work in `ℤ`.
- **`rpow` vs `pow`**: use `rpow` for real exponents, `pow` for `ℕ` exponents. Key rewrite:
  `rpow_natCast`.
- **`Finset.card_fin n`** (not `Finset.card_univ`) for
  `Finset.card (Finset.univ : Finset (Fin n)) = n`.
- **`div_le_iff₀`** (not `div_le_iff`) for the standard division-to-multiplication equivalence.
- **`push_cast` before arithmetic** to normalize `↑(a - b)` → `↑a - ↑b`.
- **Beta redexes after `unfold`**: fix with `simp only [Function.comp]` or `beta_reduce`.
- **`Function.update_same`** (not `update_self`).
- **Don't use `norm_num` on transcendentals** (exp, log). Chain bounds lemmas instead.
- **`field_simp` on sums can explode.** Use targeted rewrites instead.
- **`erw` is a last resort.** Prefer `rw` after `dsimp` or `change`.

## `unproved` vs `axiom` vs `sorry`

- **`unproved`** — "the book doesn't prove this." Use the `unproved` macro
  (`unproved theoremName (args) : Conclusion`) when the source explicitly omits the proof,
  references another source, or states without proof. Compiles to `@[unproved] axiom` so the
  infrastructure tracks it as a justified gap. **Best option** for statements without proofs in
  the source material.
- **`axiom`** — for infrastructure the worker fails to construct (definitions, structures,
  instances, helper lemmas). Compiles cleanly without poisoning the kernel. Prefer over `sorry`
  when you must move on — but it is still a gap a reviewer will weigh (see the
  **formalization-workflow** axiom policy).
- **`sorry`** — **avoid.** Introduces `sorryAx`, which breaks soundness for everything
  downstream. Use only as a temporary placeholder during active proof development, never as a
  final state.
