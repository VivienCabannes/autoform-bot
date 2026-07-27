# Mathlib and Lean 4 style runbook

Authoritative conventions for writing Mathlib-compatible Lean 4 code, distilled from community
Mathlib review practice (PR review comments and Zulip discussion). The full convention set lives
in `internal/references/mathlib/mathlib-conventions.md` — read it before writing
non-trivial Lean. The topic guides
below load on demand, so depth costs nothing while idle. This is the yardstick the code-quality
reviewer scores against and the style reference the worker writes to.

## Operating profile

Detect what's available and adapt:

- **Search before proving.** Use `exact?`, `apply?`, `rw?` in a temporary Lean
  snippet or a working REPL, or the `mathlib` MCP search tools (`mathlib_grep`,
  `mathlib_find_name`) to find existing Mathlib lemmas before reproving anything.
- **Build incrementally.** Type-check often with `lake env lean <file>` and use
  `lake build <target>` for the final check. Real LSP/REPL tools may accelerate
  feedback when available; optional compatibility stubs must not block progress.
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
  `internal/references/mathlib/mathlib-conventions.md`.
- **Style:** top-level decls at column 0, proof bodies indented 2 spaces, one tactic per line,
  no blank lines inside proofs, **100-character line width**. No statement changes without
  permission. No `elab`/`macro`/`syntax` to bypass the kernel.

## Topic reference guides (`internal/references/mathlib/`)

| Guide | When |
|---|---|
| `internal/references/mathlib/mathlib-conventions.md` | The full conventions list — read first |
| `internal/references/mathlib/lean4-syntax.md` | Lean 4 syntax gotchas vs. Lean 3 / informal math |
| `internal/references/mathlib/tactic-patterns.md` | Tactic selection and idioms |
| `internal/references/mathlib/proof-patterns.md` | Common proof shapes that recur in Mathlib |
| `internal/references/mathlib/type-coercions.md` | Coercions, `↑`, `Nat`/`Int`/`Real` casts, `push_cast`/`norm_cast` |

Analysis-specific guides (norms/bounds, derivatives/smoothness, integrals/measures) are not
included yet — they lazy-load cheaply and can be added when analysis work needs them. Keeping
Keeping `lake build` fast is covered by
`internal/references/proving/tool-usage.md`.

## Related

Pairs with `internal/runbooks/proving.md` (axioms / `sorry` / commit
discipline) and `internal/runbooks/evaluation.md` (how this code is graded). It
is the yardstick the `code-quality-reviewer` scores against, and the style
reference the `autoform-worker` writes to.
