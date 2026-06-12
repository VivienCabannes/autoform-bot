# Task Management

Stay focused on the task as stated. Avoid scope creep.

## Before starting

- Read the full task description and any task-specific guide before writing code.
- Only modify files explicitly mentioned in the task.
- Note constraints: "DO NOT add axioms", "do not modify the theorem statement" — if axioms are forbidden and proof is too hard, leaving sorry is correct.

## Blocked tasks

- If 2+ worker attempts exhaust their effort budget without a commit, the task is too hard as scoped.
- If every attempt gets the same rejection, the task can't be completed as stated.
- If workers converge on axiom introduction, the proof requires missing infrastructure. Report to the coordinating session for re-scoping.

## Effort budget

- Avoid repeated full-file diagnostics on large files (each pass is expensive); check the one file you changed.
- Prefer `lake env lean <file>` over a full `lake build` for speed.
- Batch edits and commit early. Plan all edits before making any.
