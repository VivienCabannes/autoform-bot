# Sorry handling and the FAILED rule

The minimum bar for acceptance is **net `sorry` reduction**. Count before and after:
`grep -c "sorry" file.lean`. Note that commit acceptability and task status are separate
questions: reducing 5 `sorry`s to 2 is a commit-worthy improvement *and* still status `FAILED`
for the task — the task only succeeds when the target is fully proved (see the FAILED rule
below).

## Rules

- Never decompose a single `sorry` into multiple `sorry`'d helpers — the gap count goes up, and
  a reviewer rejects when the count increases.
- Never redistribute a `sorry` by creating `sorry`'d helpers and "proving" the target from them
  — `#print axioms` (via `lake env lean`) detects `sorryAx` in the axiom list regardless of how
  the gap is spread.
- Never introduce a new `sorry` to close an existing one. If changing a definition breaks other
  proofs, fix **all** of them.
- If you cannot prove a helper, inline the attempt rather than leaving a separate `sorry`'d
  lemma.
- Leaving a `sorry` as-is is always better than shuffling it around.

## Spec phase (statements before proofs)

When a workflow formalizes statements first (a spec-gated run), theorem bodies are `sorry` *by
design* during the spec phase: each one is tracked on its node and must be eliminated — proved,
or moved to the project's sanctioned placeholder where the source omits the proof — before the
work is finished. Spec-phase `sorry`s are declared as such to reviewers; the net-reduction rules
above apply to the **proof** phase, not the declared spec stage. None of this applies to an
audited axiom ledger (see the *Axiom-discharge repos* section of `axiom-policy.md`): the axiom
layer never gains `sorry`s.

## The FAILED rule — the worker's one honest self-report

The worker's job is search → write → iterate-to-compile. It does **not** certify its own output
(verification lives in the reviewer/packet path). But there is exactly one status the worker
*must* report truthfully, and it is the spine of the whole pipeline:

> **Never deliver a `sorry`'d or axiom-stubbed file as "done." If the proof is not genuinely
> closed, return a `FAILED` status — explicitly, with the remaining gap named.**

A worker that hands back a file with a hidden `sorry`, an `axiom` standing in for the real
proof, a `decide`/`native_decide` masking an unfinished case, or a `False.elim` on a goal that
is not actually false, has cheated — even if the file compiles. The correct move when the proof
won't close is **FAILED + the named missing lemma** (see `task-management.md`), which feeds the
DAG a new node. An honest failure is useful; a disguised one poisons everything downstream.
