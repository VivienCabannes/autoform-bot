---
name: escalation-triage
description: >
  Coordinates ordered proof recovery: find an informal route and prior art,
  seek a disproof, derive reconstructible sublemmas, then broaden exploration.
kind: escalation
label: Proof recovery
icon: ⚑
blurb: research, refute, or decompose before retrying
applies: any
drained_by: agent
writes: graph
---

Recover one failed theorem without blindly retrying it and without ending at
"needs human." Read the exact Lean statement, its Markdown node, dependencies,
sources, prior recovery notes, and the worker failure.

Run these waves in order and stop on a decisive result:

1. Spawn at least two independent proof-strategy researchers plus prior-art and
   source search. A viable route must reach the exact statement and identify
   concrete Mathlib declarations or intermediate claims.
2. If no route survives comparison, run at least two independent
   counterexample searches with different edge cases. Verify a witness in Lean
   when practical.
3. If neither succeeds, run independent decompositions. Accept new lemmas only
   when they are strictly simpler and explicitly reconstruct the target.
4. Broaden methods and sources, recording checked failures so another wave does
   not repeat them.

Child researchers do not edit files. You alone update the target's mathematical
proof notes. Add accepted sublemmas as `kind: node` Markdown pages and typed
dependency links. Keep raw search logs local; preserve only the checked route,
counterexample, reconstruction, citations, and failed mathematical approaches
that help later work. Never edit generated `graph.json` directly.

End with exactly one marker:

- `RECOVERY: RETRY - <materially changed route or DAG>`
- `RECOVERY: REFUTED - <verified witness or precise defect>`
- `RECOVERY: PARK - <checked surfaces exhausted>`

Regenerate the compatibility graph after structural edits. Parking preserves a
durable research frontier; it is not a request for a person to prove the node.
