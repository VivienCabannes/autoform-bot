# Proof and formalization runbook

How to fill a node's Lean statement with a genuine proof: from reading source material through
search → write → iterate-to-compile, under a no-cheating contract, ending in an honest status.
This is the **worker's** discipline. The worker does **not** self-certify — the verification gate
(`lake env lean` + `#print axioms`) lives in the reviewer/packet path. Each topic below has a
load-bearing reference in `internal/references/proving/`; this file stays lean
and points there.

## Core workflow

1. **Search first.** Look for the lemma in Mathlib (`loogle`, `lean-explore search`,
   the local Mathlib search CLI, or `exact?`/`apply?`/`rw?`) before reproving anything — many standard results already
   exist. See `internal/references/proving/proof-strategies.md`.
2. **Prototype and compile incrementally.** Inspect goals and test fragments
   with `lean-lsp-mcp`, otherwise use a temporary Lean file and `lake env lean
   <file>`. Check the actual project file after each landed
   change and reserve full `lake build` for the final check. See
   `internal/references/proving/tool-usage.md`.
3. **Work incrementally.** Fix compilation errors first, then triage `sorry`s by difficulty
   (easy → medium → hard); prove the easy ones first.
4. **Commit often.** Each compiling step gets its own commit, named after the task
   (`convex-sets-def: formalize convex set definitions`). See
   `internal/references/proving/commit-and-submit.md`.

## The honesty gates

- **`sorry` handling and the FAILED rule.** Net `sorry` reduction is the minimum bar. Never
  decompose, redistribute, or shuffle a `sorry` into helper lemmas to hide it — `#print axioms`
  exposes `sorryAx` either way; leaving a `sorry` as-is beats shuffling it. In a spec-gated run,
  theorem bodies are `sorry` *by design* during the spec phase and declared as such; the
  net-reduction rules govern the proof phase. The one self-report that **is** the worker's job:
  **never deliver a `sorry`'d or axiom-stubbed file as done — return an honest `FAILED` status
  with the remaining gap named.** See
  `internal/references/proving/sorry-handling.md`.
- **Axiom policy.** The only acceptable kernel axioms are `propext`, `Classical.choice`,
  `Quot.sound`; anything else (or `sorryAx`) means the proof is not genuine. Never use the
  `axiom` keyword to launder a `sorry`. The second half of
  `internal/references/proving/axiom-policy.md` carries the
  **audited-ledger discharge** rules — statement **byte-identical**, ledger + machine report in
  the **same commit**, **satisfiability vetting** before any new or strengthened axiom. Read its
  *Axiom-discharge repos* section before touching any `AX_*`.
- **False statements.** If the target looks false or unprovable as written, stop and report it —
  never silently weaken it, smuggle in a hypothesis the call sites can't supply, or wrap it in a
  vacuous antecedent. A statement false as written is an escalation, not a proof problem. See
  `internal/references/proving/false-statements.md`.

## When you're blocked

A failed proof opens ordered recovery; it does not justify an unchanged retry
or immediately force a new DAG node. Report the exact Lean goal, attempted
routes, and any specific missing lemma or definition with its full statement
and types. Recovery first researches an informal route and prior art, then
seeks a disproof, then accepts sublemmas only when they reconstruct the target.
Pair the report with `FAILED`, never a disguised partial result.

## Commit and submit

Commit your first **compiling** change early; one logical step per commit. When diagnostics show
0 errors, commit and report — don't keep iterating after the build confirms correctness. When the
effort budget is nearly spent, stop and report state honestly **with an explicit gap list** —
every remaining `sorry`, stubbed helper, and `unproved` placeholder named in plain terms. Partial
progress is worth reporting; a commit that hides its gaps is the same cheat as a `FAILED` task
delivered as "done." See
`internal/references/proving/commit-and-submit.md`.

## Reference guides (`internal/references/proving/`)

| Guide | When |
|---|---|
| `internal/references/proving/proof-strategies.md` | Incremental approach, REPL prototyping, search-first |
| `internal/references/proving/tool-usage.md` | REPL / LSP / mathlib MCP usage and the build-timeout playbook |
| `internal/references/proving/sorry-handling.md` | Net-reduction rules, spec-phase sorrys, the FAILED rule |
| `internal/references/proving/axiom-policy.md` | Kernel-axiom policy + audited-ledger discharge protocol |
| `internal/references/proving/false-statements.md` | Detecting and reporting false / unprovable statements |
| `internal/references/proving/commit-and-submit.md` | Atomic commits, honest gap-listed reporting |

## Related

Pairs with `internal/runbooks/mathlib-style.md` (how to write idiomatic Mathlib
code) and `internal/runbooks/evaluation.md` (how the result is scored). The
prove path (`autoform-worker`) and the reviewers load this discipline.
