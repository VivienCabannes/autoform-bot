# False Statements

Some formalized statements are mathematically false. Detect early and handle correctly.

## Detection

- Try small parameter instantiations (n=1, zero function, degenerate cases).
- If three independent proof paths fail at the same point, investigate whether the statement is false.
- Watch for type-space confusion where the same Lean type represents two different mathematical spaces — a statement can type-check but be semantically wrong.

## Handling

- Document the counterexample in a comment, report the issue, and leave as sorry.
- Do NOT replace sorry with axiom (worse because silent) or shuffle to helper lemmas.
- Never weaken a hypothesis to make a lemma provable if call sites can't provide the stronger hypothesis — trace the full call chain first.
