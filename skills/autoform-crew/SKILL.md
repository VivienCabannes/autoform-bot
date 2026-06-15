---
name: autoform-crew
description: >
  Orchestration guide for parallel formalization with subagent teams.
  Tells the main thread WHEN and HOW to spawn autoform-worker, autoform-reviewer,
  and autoform-reader subagents for parallel proving, batch review, and
  context-efficient file reading.
  Trigger: "parallelize", "formalize chapter", "batch", "use crew",
  "spawn workers", "delegate formalization".
---

# Autoform Crew — Parallel Formalization

Autoform-crew orchestrates multiple subagents for parallel formalization. The main thread plans and coordinates; subagents do the proving, reviewing, and reading.

## Agents

| Agent | Model | Role | MCP servers |
|-------|-------|------|-------------|
| `autoform-worker` | opus | Formalize: read source, search Mathlib, write proofs | repl, mathlib, trace |
| `autoform-reviewer` | opus | Review: check faithfulness, cheating, conventions | lsp, mathlib, trace |
| `autoform-reader` | haiku | Read: summarize large files cheaply | none |

## Aristotle delegation

**Aristotle** (Harmonic) is an autonomous formal-reasoning agent — not a subagent you spawn, but a tool you call. It runs its own Lean builds, proof search, and file edits on Harmonic's servers, then returns finished files.

Use Aristotle when:
- The theorem is self-contained (no dependencies on your in-progress code)
- You want to offload a hard proof entirely — Aristotle is built for multi-hour proving sessions

Don't use Aristotle when:
- The proof depends on definitions you've written (Aristotle can't see your workspace unless you pass `project_dir`)
- You need fine-grained control over the proof approach

<!-- TODO: Add remaining Aristotle guidance (trivial lemma overhead, Aristotle tools table, Aristotle + local workers pattern, steering Aristotle). See examples/skills/autoform-crew/SKILL.md for the full version. -->

## When to use crew vs main thread

| Task | Use |
|------|-----|
| Formalize 3+ independent targets from a chapter | Parallel `autoform-worker` per target |
| Formalize 1 theorem with tricky dependencies | Main thread (needs cross-file context) |
| Review all files in a directory | Parallel `autoform-reviewer` per file |
| Read a 500-line book chapter for context | `autoform-reader` (saves main context) |
| Quick Mathlib search or REPL test | Main thread, no subagent |

**Rule of thumb:** spawn subagents for independent work that doesn't need cross-task context. Keep interdependent work in the main thread.

## Parallelization patterns

### Fan-out workers (most common)

Formalize a chapter by spawning one worker per independent target:

```
I want to formalize Chapter 3 from book.md. Here are the targets:

1. Definition 3.1 (Metric space) — probably in Mathlib already
2. Definition 3.2 (Open ball) — probably in Mathlib already
3. Theorem 3.3 (Triangle inequality for open balls) — depends on 3.1, 3.2
4. Lemma 3.4 (Open balls are open sets) — depends on 3.1, 3.2
5. Theorem 3.5 (Hausdorff property) — depends on 3.1, 3.2

Spawn autoform-worker for targets 3.3, 3.4, and 3.5 in parallel.
They all depend on 3.1 and 3.2 which are in Mathlib, so they're independent.
Write to MyBook/MetricSpaces.lean, MyBook/OpenSets.lean, MyBook/Hausdorff.lean.
```

<!-- TODO: Add remaining parallelization patterns (read-plan-fan-out, parallel review, prove-review-fix pipeline, wave-based chapter formalization). See examples/skills/autoform-crew/SKILL.md for the full version. -->

## Output contracts

**`autoform-worker` returns:**
```
Formalized: <target name>
File: <path>
Status: proved | sorry (<count>) | unproved (<count>)
Summary: <1-2 sentences on approach>
```

**`autoform-reviewer` returns:**
```
APPROVED: <brief reason>
```
or:
```
REJECTED: <reason>
Issues: <numbered list with file:line>
Fixes: <numbered list>
```

**`autoform-reader` returns:**
Structured summary with section headings, theorem names, and line numbers. Concise — its whole point is saving main-thread context.

## What NOT to do

- Don't spawn a worker for a target whose dependencies aren't formalized yet — it will waste turns trying to import nonexistent definitions.
- Don't spawn parallel workers that write to the same file — they'll conflict.

<!-- TODO: Add remaining anti-patterns (don't use reviewer for style-only checks, don't spawn reader for small files, don't expect workers to coordinate). See examples/skills/autoform-crew/SKILL.md for the full version. -->

## Maximizing parallelism

1. **Extract targets first** (`/autoform-extract`) and identify the dependency graph.
2. **Separate independent clusters** — targets that share no definitions can be parallelized.

<!-- TODO: Add remaining parallelism tips (one file per target, batch reviews, feed rejection feedback forward). See examples/skills/autoform-crew/SKILL.md for the full version. -->
