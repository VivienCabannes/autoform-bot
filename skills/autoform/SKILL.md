---
name: autoform
description: >
  Mathlib & Lean 4 conventions for writing formalization-quality code — naming, proof style,
  typeclasses, tactic selection, simp discipline, API design, coercions, and common pitfalls.
  Distilled from community Mathlib review practice (PR review comments and Zulip discussion).
  Use when editing .lean files, writing or reviewing Lean 4 / Mathlib code, searching mathlib
  for lemmas, choosing tactics, naming declarations, or formalizing mathematics. Do NOT trigger
  for Coq/Rocq, Agda, Isabelle, HOL4, Mizar, Idris, or other non-Lean provers.
  Triggers on: /autoform, "lean conventions", "mathlib style", "formalize".
version: 0.3.0
---

# Mathlib & Lean 4 conventions

Authoritative conventions for writing Mathlib-compatible Lean 4 code, distilled from community
Mathlib review practice (PR review comments and Zulip discussion). The full convention set lives
in `references/mathlib-conventions.md` — read it before writing non-trivial Lean. The topic guides
below load on demand, so depth costs nothing while idle. This is the yardstick the code-quality
reviewer scores against and the style reference the worker writes to.

## Operating profile

Detect what's available and adapt:

- **Search before proving.** Use `exact?`, `apply?`, `rw?` (inside a `run_lean_code` snippet via
  the `autoform-repl` MCP), or the `mathlib` MCP search tools (`mathlib_grep`,
  `mathlib_find_name`) to find existing Mathlib lemmas before reproving anything.
- **Build incrementally.** Type-check often: the `autoform-lsp` MCP (`lean_diagnostic_messages`,
  `lean_hover`) for the actual project file, the `autoform-repl` MCP (`run_lean_code`) for
  isolated fragments, else `lake env lean <file>` / `lake build <target>`.
- **Do not read Mathlib source by absolute path.** Use the project's mathlib search tooling
  (`mathlib_grep` / `mathlib_read_file` via MCP, else `grep` over the mathlib checkout).

## The conventions, in brief

- **Naming:** `snake_case` theorems/lemmas, `UpperCamelCase` types/classes, `lowerCamelCase`
  terms. Standard suffixes (`_iff`, `_of_`, `_inj`, `_mono`, `_left`, `_right`, `_def`,
  `_apply`). One concept, one name — check existing Mathlib names first. A namespace names a
  *mathematical topic*, never a task, declaration, or chapter.
- **Proof style:** `simp only [...]` with explicit lemma lists for non-terminal steps (bare
  `simp` only to close a goal); `calc` for chained (in)equalities; `ext`/`funext` for equality of
  functions/structures; handle trivial cases (`x = 0`, `n = 0`, `s = ∅`) first; `refine ... ?_`
  to expose subgoals; prefer API lemmas (`foo_def`, `foo_apply`) over broad `unfold`; `by
  classical` inside proofs rather than `Classical` on the statement.
- **Types & hypotheses:** weakest sufficient typeclasses (`Semiring` over `Ring`, `Preorder` over
  `LinearOrder`); `Finite` over `Fintype` when only finiteness is needed; named implicit args
  `(R := R)` over `@foo _ _ _`; remove unused hypotheses.
- **Key tactics:** `positivity` for `0 ≤/< x`, `omega` for `Nat`/`Int`, `norm_num` for concrete
  numerics, `gcongr` for monotonicity, `ring`/`field_simp` for algebra, `linarith`/`nlinarith`
  for linear arithmetic, `push_cast`/`norm_cast`/`mod_cast` for coercions. Full table in
  `references/mathlib-conventions.md`.
- **Style:** top-level decls at column 0, proof bodies indented 2 spaces, one tactic per line,
  no blank lines inside proofs, **100-character line width**. No statement changes without
  permission. No `elab`/`macro`/`syntax` to bypass the kernel.

## Topic reference guides (`references/`)

| Guide | When |
|---|---|
| `mathlib-conventions.md` | The full conventions list — read first |
| `lean4-syntax.md` | Lean 4 syntax gotchas vs. Lean 3 / informal math |
| `tactic-patterns.md` | Tactic selection and idioms |
| `proof-patterns.md` | Common proof shapes that recur in Mathlib |
| `type-coercions.md` | Coercions, `↑`, `Nat`/`Int`/`Real` casts, `push_cast`/`norm_cast` |

Analysis-specific guides (norms/bounds, derivatives/smoothness, integrals/measures) are not
included yet — they lazy-load cheaply and can be added when analysis work needs them. Keeping
`lake build` fast is covered by the **autoform-prove** skill's `tool-usage` reference.

## Related

Pairs with the **autoform-prove** skill (axioms / `sorry` / commit discipline) and
**eval-rubrics** (how this code is graded). It is the yardstick the `code-quality-reviewer`
scores against, and the style reference the `autoform-worker` writes to.
