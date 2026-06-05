# Task Management

Stay focused on the task as stated. Avoid scope creep.

## Before starting

- Read the full task description and any task-specific guide before writing code.
- Only modify files explicitly mentioned in the task.
- Note constraints: "DO NOT add axioms", "do not modify the theorem statement" — if axioms are forbidden and proof is too hard, leaving sorry is correct.

## Blocked tasks

- If 2+ workers use all 250 turns without submitting, the task is too hard.
- If every attempt gets the same rejection, the task can't be completed as stated.
- If workers converge on axiom introduction, the proof requires missing infrastructure. Report to orchestrator.

## Turn budget

- Avoid repeated `lean_diagnostic_messages` calls on large files (75s+ each).
- Use `lean_verify` instead of `lake build` for speed.
- Batch edits and commit early. Plan all edits before making any.
