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

<!-- TODO: Add remaining faithfulness rules (provably redundant hypotheses, ignore author's docstring justifications). See examples/skills/autoform-review/SKILL.md for the full version. -->

### 3. Mathematical Correctness

- Check proof logic and definitions.
- Verify definitions match the standard mathematical definitions.

<!-- TODO: Add remaining correctness checks (theorems prove what they claim, not weaker variants). See examples/skills/autoform-review/SKILL.md for the full version. -->

### 4. Conventions

- Proper imports, naming (`snake_case` for theorems, `UpperCamelCase` for types).
- Namespaces are mathematical topics, not chapter/section numbers.

<!-- TODO: Add remaining convention checks (weakest sufficient typeclasses). See examples/skills/autoform-review/SKILL.md for the full version. -->

### 5. Cheating Detection

**Actively check for these patterns:**

- **Trivial substitution** — theorem statement replaced with `True` or trivially provable proposition while keeping name/docstring. E.g., `theorem bezout_theorem : True := by trivial`.
- **Encoding theorems as definitions** — `def foo (...) : Prop := <statement>` for something the book presents as a theorem. The `def` always type-checks (a `Prop` is just a type), so nothing is proved.

<!-- TODO: Add remaining cheating patterns (smuggling assumptions, weakening content, modeling avoidance, unacknowledged sorry/axiom). See examples/skills/autoform-review/SKILL.md for the full version. -->

### 6. Unproved Declarations

Every unproved statement must use the `@[unproved]` attribute (via the `unproved` macro):

- Read the relevant book section. If the book provides a proof (even a sketch), **REJECT** — the author must prove it.
- If the book genuinely does not provide a proof, `@[unproved]` is acceptable.

<!-- TODO: Add remaining unproved rules (sorry is never acceptable, raw axiom without @[unproved] is never acceptable). See examples/skills/autoform-review/SKILL.md for the full version. -->

### 7. Additional Checks

- `native_decide` or `decide` hiding unverified computation
- Axioms beyond the standard Lean/Mathlib set — run `#check_axioms` or `lean_verify`

<!-- TODO: Add remaining additional checks (noncomputable without justification). See examples/skills/autoform-review/SKILL.md for the full version. -->

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
