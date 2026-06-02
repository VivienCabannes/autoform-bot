# Project rubric (human-curated)

This file is the **project-specific failure-mode list** for the
formalization currently in flight. It complements the generic skill
library (`skills/lean/`, `skills/mathlib/`, `skills/workflow/`) with
patterns the maintainer has identified as load-bearing for *this* book
specifically — patterns that would be too narrow to belong in the
generic skills but that recur across multiple tasks and so deserve a
single durable home.

This file is **maintained by a human**. The orchestrator, workers, and
reviewers read it but do **not** edit it (in contrast to the
machine-generated per-task guides under `skills/tasks/{task_id}/`,
which the trace analyzer writes after failures). If you find yourself
wanting to add a rule, surface it in a task report or eval comment
instead — the human will decide what to promote here.

When the rubric is empty (as on first project setup), agents should
treat it as a placeholder and rely on the generic skill library + the
orchestrator's task descriptions.

## Top-leverage open patterns

*(One bullet per pattern. Cite real declarations/files when known. Keep
the list ≤ 6 items — bloat here is more expensive than bloat anywhere
else because every agent reads it on every task.)*

- *(none recorded yet)*

## Calibration sharpening (load-bearing rules only)

*(Per-iteration corrections that should be applied across tasks. Each
bullet must apply to ≥ 2 tasks or ≥ 2 iterations to justify a slot —
single-occurrence specifics belong in the task report, not here. ≤ 6
items.)*

- *(none recorded yet)*

## What this file is NOT

- Not a generic Lean style guide (that's `skills/lean/`).
- Not a Mathlib API cheatsheet (that's `skills/mathlib/`).
- Not workflow advice for any project (that's `skills/workflow/`).
- Not a per-task instruction set (that's the task description + the
  trace-analyzer's per-task guide at `skills/tasks/{task_id}/guide.md`).
- Not a place for verbose explanations — each item should be one or
  two lines naming the pattern, the canonical instance, and the
  redirection target.
