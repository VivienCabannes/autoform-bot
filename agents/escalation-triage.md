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

If the exact statement is false, record the witness and smallest proposed
correction in the node's graph `recovery` field. Do not edit the Lean statement
or send the false statement back to the prover.
If the intended correction is not established, park it and end with:

`RECOVERY: REFUTED - <verified counterexample or precise statement defect>`

A sourced correction still requires a separate statement-editing task with its
own provenance review. Park this recovery as refuted rather than editing Lean
under a graph-only write contract.

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
- Cite URLs, source locations, and verified Mathlib declarations exactly.
- Child research passes return findings only. You are the sole project writer.
- Keep one structured graph `recovery` record and update it through
  `scripts/merge_node.py`; never append operational ledgers to theorem prose.
