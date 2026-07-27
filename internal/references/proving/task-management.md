# Escalation — a blocked worker grows the DAG

Stay focused on the node as stated; don't drift into scope creep. The point of this reference is
what to do when you **cannot finish** — because in an incremental DAG formalization, a precise
block is the signal that *creates the next node*.

## The escalation core

A worker that runs out of road is not a failure to swallow — it is information the planner needs.
The discipline:

- **Name the specific missing prerequisite.** Not "this is hard" — the exact lemma or definition
  that, if it existed, would let the proof close: a statement, with the types, as concretely as
  you can state it.
- That named gap is a **new node**. A blocked worker naming a missing lemma is precisely the
  signal that **grows the dependency graph** — the orchestrator turns the report into a new
  `missing` node with an edge into the one you were proving, and schedules it. This is how
  incremental DAG formalization discovers its own prerequisites.
- Pair the escalation with an honest `FAILED` status on the node you couldn't close (see
  `sorry-handling.md`) — never a disguised partial result.

## Signs a node is blocked, not just hard

- Two or more genuine attempts exhaust the effort budget without a compiling commit → the node
  is too hard **as scoped**; report what sub-result *would* unblock it.
- Every attempt hits the same wall (same missing fact, same failing step) → the proof needs
  **missing infrastructure**; name that infrastructure as the new prerequisite.
- You find yourself reaching for an `axiom` or a fresh `sorry` to "move on" → stop. That is the
  cue to escalate the underlying gap instead of laundering it.

## Effort budget

- Don't burn the budget on repeated full-file diagnostics — check the one file you changed.
- Prefer `lake env lean <file>` / a single-declaration REPL check over a full `lake build`.
- Plan your edits, batch them, and commit early; when the budget is nearly spent, stop and
  report state honestly (→ `commit-and-submit.md`) rather than polishing cosmetics.
