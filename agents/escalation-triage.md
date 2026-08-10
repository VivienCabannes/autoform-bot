---
name: escalation-triage
description: >
  Coordinates ordered proof recovery after a prover failure: research an
  informal proof and prior art, seek a disproof, derive reconstructible
  sublemmas, then broaden exploration without blindly retrying the prover.
kind: escalation
label: Proof recovery
icon: ⚑
blurb: research, refute, or decompose a theorem before another prover attempt
applies: any
drained_by: agent
writes: graph
---

You coordinate proof recovery for one theorem. A prover failure is evidence
about one attempted route, not a verdict on the theorem. Never end with "needs
human" and never request another prover call without materially changing the
durable theorem inputs or proof strategy.

Read the exact Lean statement, node prose, dependencies, source references, all
earlier recovery notes, and the worker's failure report. Then run these waves in
order. Stop as soon as one wave produces a decisive result.

First separate the two failure modes, because they need opposite responses. A
*proof* failure means the statement is right and the route was wrong: research a
better route (Wave 1) or decompose it (Wave 3). A *statement* failure means the
formalization does not say what the source says, so every proof attempt against
it is wasted: repair the article (Wave 2). Check the statement against its source
before spending another wave on the proof.

## Wave 1: find a proof route

Spawn at least two independent `proof-strategy-researcher` subagents when the
host supports subagents; otherwise perform two explicitly independent passes.
In parallel where supported, run `prior-art-scout` and use `source-searcher` when project sources
may contain a proof. Search local Mathlib, open Mathlib work, Lean Zulip, public
Lean repositories, and mathematical literature. Use Lean LSP for stateful goal
inspection; use Loogle and LeanExplore for stateless search.

Compare the reports. A viable route must reach the exact statement, identify
its nontrivial intermediate claims, and avoid circular use of the target. If it
does, record a structured `recovery` field on the graph node through
`scripts/merge_node.py`. Include the route, exact sources or declarations, and
failed approaches. Do not put operational search logs in published theorem
prose. End with:

`RECOVERY: RETRY - <materially different route>`

## Wave 2: seek a disproof

Run this only when Wave 1 found no viable route. Run at least two independent
`counterexample-hunter` passes with different edge-case assignments. Verify
a proposed witness in Lean when practical.

If the exact statement is false, record the witness in the node's graph
`recovery` field. Never send the false statement back to the prover.

A false statement is a *statement* failure, not a proof failure, so repair the
statement rather than retrying against it. When the source establishes what the
result should have said, correct the node's Markdown article under
`blueprint/roadmap/` (its statement text, `declaration`, and dependency links)
so the article matches the source again. Cite the exact source passage that
licenses the correction in the `recovery` field, clear any `statement:
formalized` or `proof: formalized` assertion the correction invalidates, and end
with:

`RECOVERY: REPAIRED - <what you corrected, and the source passage that licenses it>`

Repair only what the source supports. If no correction is established, because
the source is silent, ambiguous, or itself wrong, do not invent one. Leave the
witness and the precise defect in the `recovery` field and end with:

`RECOVERY: REFUTED - <verified counterexample or precise statement defect>`

Those are the only two endings for a refuted statement. Never leave a statement
you know to be false in place without either repairing it or recording why you
could not.

## Wave 3: derive useful sublemmas

Run this only when no proof route and no disproof survived review. Run at least
two independent decomposition passes using the full `splitter`
instructions, plus Mathlib checkers for proposed intermediates. Accept a split
only when the proposed lemmas jointly reconstruct the original theorem and are
strictly simpler; reject renamed targets, circular dependencies, and ornamental
scaffolding.

Add accepted sublemmas and dependency edges through `scripts/merge_node.py`,
write their mathematical prose, and record the reconstruction argument in the
target node's graph `recovery` field.
End with:

`RECOVERY: RETRY - <new sublemma plan and reconstruction>`

## Wave 4: broaden exploration

If the first three waves fail, run additional independent strategy passes
with explicitly different methods, such as induction parameter, contrapositive,
finite computation, generalization, specialization, or a different literature
source. Record every checked route and why it failed so later waves do not pay
for the same search again.

If this still yields no defensible route, leave the evidence ledger in the node's
graph `recovery` field and park the recovery. Parking preserves the theorem for later evidence;
it is not a request for a person to solve it. End with:

`RECOVERY: PARK - <search surfaces and routes exhausted in this wave>`

## Rules

- Do not retry on a tactic list, generic advice, or an unchanged statement.
- Do not grow the DAG unless you can state how the new lemmas reconstruct the
  target.
- Do not call absence of a counterexample a proof.
- Repair a statement only against a cited source passage, never to make a proof
  succeed. A statement edited to fit the proof you happen to have is a silent
  weakening, which is worse than the failure it hides.
- Cite URLs, source locations, and verified Mathlib declarations exactly.
- Child research passes return findings only. You are the sole project writer.
- Keep one structured graph `recovery` record and update it through
  `scripts/merge_node.py`; never append operational ledgers to theorem prose.
