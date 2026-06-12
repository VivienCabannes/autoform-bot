# Axiom Policy

**Precedence:** if the repo keeps an audited axiom ledger (see `axiom-discharge.md`), that
protocol overrides this file — never convert a ledgered axiom to a sorry'd theorem, never
commit sorries against the axiom layer; a discharge replaces the axiom with a sorry-free proof
of the *verbatim* statement.

Never use the `axiom` keyword to replace `sorry` — reviewers reject 100% of the time (cheating pattern f).

## Rules (ordinary projects, no audited ledger)

- If a stray axiom exists in the code, convert it to `theorem ... := by sorry` preserving the exact signature — and then attempt the proof; the conversion alone is not progress.
- When decomposing, split into genuinely distinct sub-results, not weaker versions of the main theorem. Each piece should imply only part of the solution.
- Some reviewers accept new axioms if mathematically sound; others reject all. When uncertain, use `sorry` in a theorem instead.
- Never shuffle axioms (rename, split, or recombine without proving anything). Reviewers check axiom lists via `#print axioms` (`lake env lean`).
