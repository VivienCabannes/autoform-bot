---
name: autoform-prove
description: >
  Lean 4 proof strategies and the worker's formalization workflow, end-to-end. Incremental
  proving, REPL/LSP prototyping, search-before-proving, sorry handling and the FAILED rule,
  axiom policy and audited-ledger discharge, false-statement detection, escalation that grows
  the DAG, and honest commits. Use when proving theorems, debugging proofs, handling
  sorry/axiom, dealing with slow or timing-out builds, escalating a blocked task, or
  committing formalized code in Lean 4. Do NOT trigger for non-Lean theorem provers.
  Triggers on: /autoform-prove, "prove this", "sorry handling", "proof strategy".
version: 0.3.0
---

# Proof strategies & formalization workflow

How to fill a node's Lean statement with a genuine proof: from reading source material through
search → write → iterate-to-compile, under a no-cheating contract, ending in an honest status.
This is the **worker's** discipline. The worker does **not** self-certify — the verification gate
(`lake env lean` + `#print axioms`) lives in the reviewer/packet path. Each topic below has a
load-bearing reference in `references/`; this file stays lean and points there.

## Core workflow (`proof-strategies.md`, `tool-usage.md`)

1. **Search first.** Look for the lemma in Mathlib (`mathlib_grep`, `mathlib_find_name`, or
   `exact?`/`apply?`/`rw?` in the REPL) before reproving anything — many standard results already
   exist. See `proof-strategies.md`.
2. **Prototype in the REPL.** Test proof fragments with `run_lean_code` (the `autoform-repl` MCP)
   before editing large files; only write to the file once a fragment compiles. Use the
   `autoform-lsp` MCP (`lean_diagnostic_messages`, `lean_hover`) for files with custom defs the
   REPL can't see, and reserve full `lake build` for final checks. See `tool-usage.md`.
3. **Work incrementally.** Fix compilation errors first, then triage `sorry`s by difficulty
   (easy → medium → hard); prove the easy ones first.
4. **Commit often.** Each compiling step gets its own commit, named after the task
   (`convex-sets-def: formalize convex set definitions`). See `commit-and-submit.md`.

## The honesty gates (`sorry-handling.md`, `axiom-policy.md`, `false-statements.md`)

- **`sorry` handling and the FAILED rule.** Net `sorry` reduction is the minimum bar. Never
  decompose, redistribute, or shuffle a `sorry` into helper lemmas to hide it — `#print axioms`
  exposes `sorryAx` either way; leaving a `sorry` as-is beats shuffling it. In a spec-gated run,
  theorem bodies are `sorry` *by design* during the spec phase and declared as such; the
  net-reduction rules govern the proof phase. The one self-report that **is** the worker's job:
  **never deliver a `sorry`'d or axiom-stubbed file as done — return an honest `FAILED` status
  with the remaining gap named.** See `sorry-handling.md`.
- **Axiom policy.** The only acceptable kernel axioms are `propext`, `Classical.choice`,
  `Quot.sound`; anything else (or `sorryAx`) means the proof is not genuine. Never use the
  `axiom` keyword to launder a `sorry`. The second half of `axiom-policy.md` carries the
  **audited-ledger discharge** rules — statement **byte-identical**, ledger + machine report in
  the **same commit**, **satisfiability vetting** before any new or strengthened axiom. Read its
  *Axiom-discharge repos* section before touching any `AX_*`.
- **False statements.** If the target looks false or unprovable as written, stop and report it —
  never silently weaken it, smuggle in a hypothesis the call sites can't supply, or wrap it in a
  vacuous antecedent. A statement false as written is an escalation, not a proof problem. See
  `false-statements.md`.

## When you're blocked (`task-management.md`)

A blocked worker is not a dead end — it is a **signal that grows the DAG**. When you cannot
finish because a specific lemma or definition is missing, **name that missing prerequisite
precisely** (statement + types) and report it. The orchestrator turns that named gap into a new
node with an edge into the one you were proving. Pair it with an honest `FAILED` status — never a
disguised partial result. See `task-management.md`.

## Commit and submit (`commit-and-submit.md`)

Commit your first **compiling** change early; one logical step per commit. When diagnostics show
0 errors, commit and report — don't keep iterating after the build confirms correctness. When the
effort budget is nearly spent, stop and report state honestly **with an explicit gap list** —
every remaining `sorry`, stubbed helper, and `unproved` placeholder named in plain terms. Partial
progress is worth reporting; a commit that hides its gaps is the same cheat as a `FAILED` task
delivered as "done." See `commit-and-submit.md`.

## Reference guides (`references/`)

| Guide | When |
|---|---|
| `proof-strategies.md` | Incremental approach, REPL prototyping, search-first |
| `tool-usage.md` | REPL / LSP / mathlib MCP usage and the build-timeout playbook |
| `sorry-handling.md` | Net-reduction rules, spec-phase sorrys, the FAILED rule |
| `axiom-policy.md` | Kernel-axiom policy + audited-ledger discharge protocol |
| `false-statements.md` | Detecting and reporting false / unprovable statements |
| `task-management.md` | Escalation — naming the missing lemma to grow the DAG |
| `commit-and-submit.md` | Atomic commits, honest gap-listed reporting |

## Related

Pairs with **autoform** (how to write idiomatic Mathlib code) and **eval-rubrics** (how the
result is scored). The prove path (`autoform-worker`) and the reviewers load this discipline.
