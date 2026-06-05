---
name: lean-conventions
description: >-
  Use when editing .lean files, writing or reviewing Lean 4 / Mathlib code, searching mathlib
  for lemmas, choosing tactics, naming declarations, or formalizing mathematics — especially
  analysis-flavored work (norms, bounds, derivatives, smoothness, integrals, measures,
  coercions). Provides idiomatic Mathlib conventions distilled from ~94k PR review comments and
  ~165k Zulip messages, plus topic reference guides. Do NOT trigger for Coq/Rocq, Agda,
  Isabelle, HOL4, Mizar, Idris, or other non-Lean provers.
---

# Lean 4 / Mathlib conventions

Authoritative conventions for writing Mathlib-compatible Lean 4 code. The full convention set
lives in `references/mathlib-conventions.md` (distilled from 792 community conventions); read it
before writing non-trivial Lean. The topic guides below are loaded on demand.

## Operating profile

Detect what's available and adapt (mirrors the autoform-bot worker's assumptions):

- **Search before proving.** Use `exact?`, `apply?`, `rw?` (or the Lean LSP MCP search tools if
  present) to find existing Mathlib lemmas before reproving anything.
- **Build incrementally.** Type-check often via the Lean LSP MCP tools when available, else
  `lake env lean <file>` / `lake build <target>`.
- **Do not read Mathlib source by absolute path.** Use the project's mathlib search tooling
  (`mathlib_grep` / `mathlib_read_file` when exposed via MCP, else `grep` over the mathlib
  checkout).

## The conventions, in brief

- **Naming:** `snake_case` theorems/lemmas, `UpperCamelCase` types/classes, `lowerCamelCase`
  terms. A namespace names a *mathematical topic*, never a task, declaration, or chapter. Use
  standard suffixes (`_iff`, `_of_`, `_mono`, `_left`, `_def`, `_apply`). Reuse existing
  namespaces — check before inventing.
- **Proof style:** `simp only [...]` with explicit lemma lists for non-terminal steps; `calc`
  for chained (in)equalities; `ext`/`funext` for equality of functions/structures; handle
  trivial cases (`x = 0`, `s = ∅`) first; `refine ... ?_` to expose subgoals; `by classical`
  inside proofs rather than `Classical` on the statement.
- **Types & hypotheses:** weakest sufficient typeclasses (`Semiring` over `Ring`); `Finite` over
  `Fintype` when only finiteness is needed; named implicit args `(R := R)` over `@foo _ _ _`;
  remove unused hypotheses.
- **100-character line width.** No statement changes without permission. No `elab`/`macro`/
  `syntax` to bypass the kernel.

## Topic reference guides (`references/`)

| Guide | When |
|---|---|
| `mathlib-conventions.md` | The full conventions list — read first |
| `lean4-syntax.md` | Lean 4 syntax gotchas vs. Lean 3 / informal math |
| `tactic-patterns.md` | Tactic selection and idioms |
| `proof-patterns.md` | Common proof shapes that recur in Mathlib |
| `type-coercions.md` | Coercions, `↑`, `Nat`/`Int`/`Real` casts, `push_cast`/`norm_cast` |
| `norms-bounds.md` | Norms, inequalities, bound-chasing |
| `derivatives-smoothness.md` | `deriv`, `fderiv`, `ContDiff`, smoothness |
| `integrals-measures.md` | Measure theory and integration API |
| `build-performance.md` | Keeping `lake build` fast; avoiding slow elaboration |

## Related

Pairs with the **formalization-workflow** skill (axioms / `sorry` / commit discipline) and
**eval-rubrics** (how this code will be graded). Used by the `autoform` plugin.
