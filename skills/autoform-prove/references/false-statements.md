# False statements

Some node statements are mathematically false, or false *as currently written*. Detect this
early and handle it correctly — a false target is a planning problem, not a proof problem.

## Detection

- Try small parameter instantiations (n = 1, the zero function, degenerate or empty cases). A
  statement that fails on a trivial instance is false as stated.
- If three independent proof paths fail at the same step, stop and investigate whether the
  statement itself is false rather than attacking it a fourth way.
- Watch for type-space confusion, where one Lean type stands for two different mathematical
  spaces — a statement can type-check yet be semantically wrong.

## Handling

- Document the counterexample in a comment, report the issue, and **leave the body as `sorry`**.
  Do not "fix" a false statement by proving a weaker one.
- Do **not** replace the `sorry` with `axiom` (worse, because it is silent) and do **not**
  shuffle the gap into helper lemmas.
- **Never weaken a hypothesis** to make a lemma provable if the call sites cannot supply the
  stronger hypothesis — trace the full call chain first. A locally convenient weakening that
  breaks every downstream user is not progress.
- A statement that is false as written is an **escalation**: report the precise reason it is
  false (see `task-management.md`). If the statement came from a node, that node's *statement*
  needs to change — a worker does not silently restate the target.
