---
name: formalization-workflow
description: >-
  Use when running a Lean 4 formalization task end-to-end — writing or filling proofs,
  deciding how to handle `sorry` or omitted proofs, checking axioms with `#print axioms`,
  dealing with slow or timing-out builds, escalating a blocked task, or committing and
  submitting formalized code. Provides the worker's honesty discipline (no false statements,
  sorry-handling and the FAILED rule, axiom policy and discharge, proof strategies, REPL/LSP
  tool usage, escalation, honest commits) distilled from autoform-bot's worker agent. Do NOT
  trigger for non-Lean theorem provers.
---

# Formalization workflow discipline

The process side of filling a node's Lean statement with a genuine proof — what to do while
proving, how to stay honest, and how to report. This is the **worker's** discipline: search →
write → iterate-to-compile, under a no-cheating contract, ending in an honest status. The
verification gate (`lake env lean` + `#print axioms`) lives in the reviewer/packet path, not
here — the worker never self-certifies. Each topic below has a load-bearing reference in
`references/`.

## While proving (`proof-strategies.md`, `false-statements.md`)

- **Search before proving.** Look for the lemma in Mathlib (`mathlib_grep`, `mathlib_find_name`,
  or `exact?`/`apply?`/`rw?` in the REPL) before reproving anything — see `proof-strategies.md`.
- **Build incrementally.** Prototype proof fragments with `run_lean_code`; only write to the
  file once a fragment is known to compile.
- **If the target statement looks false or unprovable as written, stop and report it**
  (`false-statements.md`) — never silently weaken it, smuggle in a hypothesis call sites can't
  supply, or wrap it in a vacuous antecedent.

## The honesty gates (`axiom-policy.md`, `sorry-handling.md`)

- **Axiom policy.** The only acceptable kernel axioms are `propext`, `Classical.choice`,
  `Quot.sound`. Anything else (or `sorryAx`) means the proof is not genuine. Never use the
  `axiom` keyword to launder a `sorry`. See `axiom-policy.md` — whose second half also carries
  the **axiom-discharge** rules for audited-ledger repos (statement byte-identical, ledger +
  report in the same commit, satisfiability vetting before any new/strengthened axiom).
- **`sorry` handling and the FAILED rule.** Net `sorry` reduction is the minimum bar. Never
  shuffle a `sorry` into helper lemmas to hide it (`#print axioms` exposes `sorryAx` either
  way). And the one self-report that *is* the worker's job: **never deliver a `sorry`'d or
  axiom-stubbed file as done — return an honest `FAILED` status instead.** See
  `sorry-handling.md`.

## Tooling and builds (`tool-usage.md`)

- Use the `autoform-repl` MCP (`run_lean_code`) for fast prototyping and the `autoform-lsp` MCP
  (`lean_diagnostic_messages`, `lean_hover`) for file-level diagnostics; reserve full `lake
  build` for final checks. `tool-usage.md` also folds in the build-timeout playbook — what to do
  when a large file times out (narrow the target, `set_option maxHeartbeats`, prototype in
  isolation).

## When you're blocked (`task-management.md`)

- A blocked worker is not a dead end — it is a **signal that grows the DAG**. When you cannot
  finish because a specific lemma or definition is missing, **name that missing prerequisite
  precisely** and report it. That named gap becomes a new node the planner can schedule. This is
  the escalation core; see `task-management.md`.

## Commit and submit (`commit-and-submit.md`)

- Commit atomically once the file compiles; one logical step per commit. When the effort budget
  is spent, **stop and report state honestly with an explicit gap list** — partial progress is
  worth reporting, but never disguise it as done. This is the persistence twin of the FAILED
  rule; see `commit-and-submit.md`.

## Axiom-discharge repos (`axiom-policy.md`)

- Repos with an audited axiom layer (ledger + per-axiom plans + soundness CI) carry stricter,
  conditional rules: the statement must stay **byte-identical** through a discharge, the ledger
  and machine report update **in the same commit**, and any new or strengthened axiom needs
  **satisfiability vetting** first. Read the *Axiom-discharge repos* section of `axiom-policy.md`
  before touching any `AX_*`.

## Related

Pairs with **lean-conventions** (how to write idiomatic Mathlib code) and **eval-rubrics** (how
the result is scored). The prove path (`autoform-worker`) and the reviewers load this discipline.
