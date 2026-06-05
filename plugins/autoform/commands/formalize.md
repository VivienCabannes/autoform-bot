---
description: Formalize one target into Lean 4 in-session (write → review-gate → merge).
argument-hint: "<target> --book-dir DIR --repo-dir DIR [--review-cycles N]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task
---

# /autoform:formalize — single-target loop

Formalize **one** target into Lean 4 in this Claude session, then gate it through correctness +
quality review before it lands. This is the lightweight, single-statement counterpart to
`/autoform:orchestrate` (which runs the full autoform-bot engine over a whole book) — use it for a
one-off target, a quick experiment, or a fix, with no autoform-bot checkout required. Arguments:
`$ARGUMENTS`.

Resolve and **echo**: the target (name/slug, e.g. from `targets.yaml`), book dir, Lean repo dir,
review-cycle cap (default 2). Load the **lean-conventions**, **formalization-workflow**, and
**eval-rubrics** skills.

**Phase 0 — Locate.** Read `lakefile.toml` for the `[[lean_lib]]` name (your source dir). Read the
target's source section in the book. Search Mathlib for what already exists.

**Phase 1 — Write (worker).** Dispatch the **worker** subagent with a self-contained task:
objective, pasted source statement, target file under `<LibName>/`, notes (Mathlib APIs, prior
decls). The worker writes code and builds incrementally.

**Phase 2 — Review gate (both must pass).** In parallel: **code-reviewer** (compiles? faithful to
the book? no cheats / clean axioms?) and **quality-inspector** (Mathlib idiom only). If either
returns `REJECTED`, loop the feedback to a fresh **worker** turn up to `--review-cycles`; record
each cycle in a **gate table** (cycle | reviewer | verdict | reason).

**Phase 3 — Merge.** Only when both approve: `lake build` green, then commit named after the
target slug (`<slug>: …`). On build failure, return to Phase 1.

**Phase 4 — Verify.** Run `/autoform:eval --backend native <decl>` and print the scorecard.

## Required artifacts

The gate table, the final commit hash, and the Phase 4 scorecard. No `sorry`/raw `axiom` may
remain (only the `unproved` macro, and only where the book omits the proof).

## Scaling up

For a whole book, use `/autoform:orchestrate` — the autoform-bot engine plans its own DAG and runs
a worker pool with a merge queue, rather than one target at a time.
