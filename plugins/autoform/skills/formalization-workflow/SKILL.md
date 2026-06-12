---
name: formalization-workflow
description: >-
  Use when running a Lean 4 formalization task end-to-end — before/after writing proofs,
  deciding how to handle `sorry` or omitted proofs, checking axioms, dealing with slow or
  timing-out `lake build`, committing and submitting formalized code, or reviewing someone
  else's formalization. Provides the workflow discipline (pre-work checks, axiom policy, sorry
  handling, proof strategies, commit/submit conventions, review patterns, false-statement
  detection) distilled from autoform-bot's worker and reviewer agents. Do NOT trigger for
  non-Lean theorem provers.
---

# Formalization workflow discipline

The process side of formalizing mathematics in Lean 4 — what to do before, during, and after
writing proofs. Each phase has a reference guide in `references/`.

## Phase 0 — Before you write code (`pre-work-checks.md`)

- Read the relevant `lakefile.toml` to find the `[[lean_lib]]` name — that is your source
  directory.
- Check for prior lessons (autoform-bot writes these to `skills/tasks/<id>/`); read them.
- Search Mathlib for what already exists before formalizing anything new.

## Phase 1 — While proving (`proof-strategies.md`, `false-statements.md`)

- Build incrementally; keep the goal state visible.
- If a target statement appears **false or unprovable as written**, stop and report it
  (`false-statements.md`) — do not silently weaken it or wrap it in a vacuous hypothesis.

## Phase 2 — Honesty gates (`axiom-policy.md`, `sorry-handling.md`)

- **Axiom policy:** the only acceptable axioms are `propext`, `Classical.choice`, `Quot.sound`.
  Anything else (or `sorryAx`) means the proof is not genuine — verify with `#print axioms`.
- **`sorry` handling:** never ship `sorry` or raw `axiom` as a finished proof. Use the project's
  sanctioned placeholder (e.g. `unproved`) only for book-omitted proofs, and flag it.

## Phase 3 — Builds (`build-timeout.md`, `tool-usage.md`)

- When `lake build` is slow or times out, narrow the target, split files, and prefer
  incremental checking; `build-timeout.md` has the playbook. `tool-usage.md` covers using the
  Lean LSP / REPL tooling efficiently.

## Phase 4 — Commit & submit (`commit-and-submit.md`, `task-management.md`)

- Name commits after the task/topic; keep diffs scoped; follow the submit/merge protocol.

## Phase 5 — The reviewer packet (`reviewer-packet.md`)

- Finished work ships with a packet a human expert can verify in minutes: spec sheet
  (statements = the trust surface), kernel evidence (`lake env lean`, `#print axioms` deltas),
  faithfulness argument, and a 5-minute reading guide. Spec-first discipline makes it cheap.

## Axiom-discharge repos (`axiom-discharge.md`)

- Challenge repos with an audited axiom layer (ledger + per-axiom plans + soundness CI) have
  stricter rules: statement must stay verbatim, ledger/report updates land in the same commit,
  new axioms need satisfiability vetting. Read this before touching any `AX_*`.

## Reviewing others' work (`review-patterns.md`)

- The correctness reviewer checks faithfulness to the source and genuine proofs; the quality
  inspector checks Mathlib idiom. `review-patterns.md` captures both lenses.

## Related

Pairs with **lean-conventions** (how to write the code) and **eval-rubrics** (how it's scored).
The `autoform:run` and `autoform:review` commands lean on this discipline.
