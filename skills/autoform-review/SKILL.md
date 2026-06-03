---
name: autoform-review
description: >
  Review Lean 4 formalization for correctness, faithfulness to source material,
  and cheating patterns. Structured checklist for code review of Lean proofs.
  Use when reviewing Lean code, checking formalization quality, or auditing proofs.
  Triggers on: /autoform-review, "review this lean", "check formalization".
---

# Formalization Review

Structured review for Lean 4 formalizations. Evaluate correctness, faithfulness, and integrity.

## Review Checklist

### 1. Compilation

Run `lean_diagnostic_messages` on changed files. Code must compile cleanly.

### 2. Faithfulness to Source

Compare the formalization to the original source material directly:

- The theorem statement must match the book's statement.
- Extra hypotheses not present in the book are **deviations, not justifications**. Do not accept "this is needed for the proof."
- The only acceptable extra hypotheses are those provably redundant (derivable from the book's hypotheses in Mathlib).
- Do not be swayed by the author's own docstrings justifying a deviation. Evaluate against the source directly.

### 3. Mathematical Correctness

- Check proof logic and definitions.
- Verify definitions match the standard mathematical definitions.
- Ensure theorems prove what they claim (not a weaker variant).

### 4. Conventions

- Proper imports, naming (`snake_case` for theorems, `UpperCamelCase` for types).
- Namespaces are mathematical topics, not chapter/section numbers.
- Weakest sufficient typeclasses.

### 5. Cheating Detection

**Actively check for these patterns:**

- **Trivial substitution** — theorem statement replaced with `True` or trivially provable proposition while keeping name/docstring. E.g., `theorem bezout_theorem : True := by trivial`.
- **Encoding theorems as definitions** — `def foo (...) : Prop := <statement>` for something the book presents as a theorem. The `def` always type-checks (a `Prop` is just a type), so nothing is proved.
- **Smuggling assumptions into structure fields** — structure fields include what should be proved as theorems. Anything the book states as "Theorem", "Proposition", "Corollary", or "Lemma" must be a separate Lean theorem — never a class field itself.
- **Weakening mathematical content** — proving a weaker numerical shadow instead of the actual result. Ask: could someone state and prove this without knowing the mathematics? If yes, the formalization is not capturing the actual theorem.
- **Modeling avoidance** — replacing mathematical objects (manifolds, schemes, sheaves) with simpler algebraic proxies without proving the proxy faithfully represents the real object.
- **Unacknowledged sorry/axiom** — sorry in helper lemmas called by "proved" theorems. Always `grep` for `sorry` and `axiom` across the **entire** project, not just the main files.

### 6. Unproved Declarations

Every unproved statement must use the `@[unproved]` attribute (via the `unproved` macro):

- Read the relevant book section. If the book provides a proof (even a sketch), **REJECT** — the author must prove it.
- If the book genuinely does not provide a proof, `@[unproved]` is acceptable.
- `sorry` is never acceptable — **REJECT**. It poisons the kernel with `sorryAx`.
- Raw `axiom` without `@[unproved]` is never acceptable — **REJECT**.

### 7. Additional Checks

- `native_decide` or `decide` hiding unverified computation
- Axioms beyond the standard Lean/Mathlib set — run `#check_axioms` or `lean_verify`
- `noncomputable` without justification

## Response Format

If the code is good:
```
APPROVED: <brief reason>
```

If the code needs fixes:
```
REJECTED: <specific, actionable feedback>

Issues found:
1. <specific issue with file path and line numbers>

Suggested fixes:
1. <how to fix>
```

Be specific — the author needs to know exactly what to fix and how.
