# Sorry Handling

The minimum bar for acceptance is net sorry reduction. Count sorries before and after:
`grep -c "sorry" file.lean`.

## Rules

- Never decompose a single sorry into multiple sorry'd helpers — reviewers reject if sorry count increases.
- Never redistribute sorry by creating sorry'd helpers and proving the target from them — `#print axioms` (via `lake env lean`) detects `sorryAx` in the axiom list.
- Never introduce a new sorry to close an existing one. If changing a definition breaks other proofs, fix ALL of them.
- If you can't prove a helper, inline the attempt instead of leaving it as a separate sorry'd lemma.
- Leaving a sorry as-is is always better than shuffling it around.

## Spec phase (statements before proofs)

When a workflow formalizes statements first (spec-gated runs), theorem bodies are `sorry` *by
design* during the spec phase: each one is tracked in the plan and must be eliminated (proved,
or moved to the project's sanctioned placeholder where the source omits the proof) before the
work is finished. Spec-phase sorries are declared as such to reviewers — the net-reduction
rules above apply to the proof phase, not to the declared spec stage. None of this applies to
an audited axiom ledger (see `axiom-discharge.md`): the axiom layer never gains sorries.
