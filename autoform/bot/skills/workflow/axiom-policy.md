# Axiom Policy

Never use the `axiom` keyword to replace `sorry` — reviewers reject 100% of the time (cheating pattern f).

## Rules

- If an axiom exists in the code, convert it to `theorem ... := by sorry` preserving the exact signature.
- When decomposing, split into genuinely distinct sub-results, not weaker versions of the main theorem. Each piece should imply only part of the solution.
- Axiom-to-sorry conversion alone is not progress — you must also attempt the proof.
- Some reviewers accept new axioms if mathematically sound; others reject all. When uncertain, use `sorry` in a theorem instead.
- Never shuffle axioms (rename, split, or recombine without proving anything). Reviewers check axiom lists via `lean_verify`.
