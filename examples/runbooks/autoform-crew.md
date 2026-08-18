# Archival crew-orchestration runbook

Autoform-crew orchestrates multiple subagents for parallel formalization. The main thread plans and coordinates; subagents do the proving, reviewing, and reading.

## Agents

| Agent | Model | Role | MCP servers |
|-------|-------|------|-------------|
| `autoform-worker` | opus | Formalize: read source, search Mathlib, write proofs | repl, mathlib, trace |
| `faithfulness-reviewer` | opus | Judge: statement captures the source at full strength | lsp, zulip |
| `proof-integrity-reviewer` | opus | Judge: proof chain is genuine work on sound foundations | lsp, zulip |
| `code-quality-reviewer` | opus | Judge: Mathlib conventions and idiomatic Lean 4 style | lsp, mathlib |
| `autoform-reader` | haiku | Read: summarize large files cheaply | none |

The three reviewer judges are blind single-axis jurors sharing the
`internal/rubrics/` data; each
returns a 0–5 score for its own rubric, and the verdict (clean / flagged / rejected) is gated
downstream.

## Aristotle delegation

**Aristotle** (Harmonic) is an autonomous formal-reasoning backend, selected
through the same unified prover as Claude and Codex. Use
`prove_node(..., backend="aristotle")`, or select `aristotle` before launching
the durable dispatcher. The shared driver handles polling, in-flight steering,
result landing, and deterministic verification; there is no separate Aristotle
MCP surface.

## When to use crew vs main thread

| Task | Use |
|------|-----|
| Formalize 3+ independent targets from a chapter | Parallel `autoform-worker` per target |
| Formalize 1 theorem with tricky dependencies | Main thread (needs cross-file context) |
| Review all files in a directory | Parallel review jury (the three single-axis reviewers) per file |
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

### Read → plan → fan-out

When you haven't read the chapter yet:

1. Spawn `autoform-reader` on the book chapter — get a structured summary cheaply
2. Main thread reads `targets.yaml`, identifies the dependency graph
3. Fan out `autoform-worker` on targets whose dependencies are all resolved

### Parallel review

After a batch of formalizations:

```
Review these 4 files against Chapter 3 of book.md:
- MyBook/MetricSpaces.lean
- MyBook/OpenSets.lean
- MyBook/Hausdorff.lean
- MyBook/Completeness.lean

Spawn the review jury — faithfulness-reviewer, proof-integrity-reviewer, and
code-quality-reviewer — for each file in parallel.
```

### Pipeline: prove → review → fix

For each target:
1. `autoform-worker` formalizes and writes the file
2. the review jury scores it against the source — `faithfulness-reviewer` (statement) and `proof-integrity-reviewer` (proof), plus `code-quality-reviewer` (style)
3. If rejected: main thread reads the feedback, spawns another `autoform-worker` with the feedback as context

### Wave-based chapter formalization

For a chapter with a dependency tree:

**Wave 1:** Spawn workers for all leaf targets (no dependencies beyond Mathlib).
**Wave 2:** Once wave 1 completes, spawn workers for targets that depended on wave 1.
**Wave 3:** Continue up the dependency tree.

```
Chapter 5 dependency graph:
  Wave 1 (parallel): def-5.1, def-5.2, def-5.3
  Wave 2 (parallel, after wave 1): thm-5.4 (needs 5.1, 5.2), lem-5.5 (needs 5.2, 5.3)
  Wave 3 (after wave 2): thm-5.6 (needs 5.4, 5.5)

Start wave 1: spawn autoform-worker for def-5.1, def-5.2, def-5.3 in parallel.
```

## Output contracts

**`autoform-worker` returns:**
```
Formalized: <target name>
File: <path>
Status: proved | sorry (<count>) | unproved (<count>)
Summary: <1-2 sentences on approach>
```

**Each reviewer judge (`faithfulness-reviewer` / `proof-integrity-reviewer` / `code-quality-reviewer`) returns strict JSON:**
```
{"score": <0-5>, "reasoning": "<evidence-grounded justification with file:line>"}
```
The per-axis scores are combined downstream into the threshold-gated verdict
(clean / flagged / rejected) per the internal rubric data.

**`autoform-reader` returns:**
Structured summary with section headings, theorem names, and line numbers. Concise — its whole point is saving main-thread context.

## What NOT to do

- Don't spawn a worker for a target whose dependencies aren't formalized yet — it will waste turns trying to import nonexistent definitions.
- Don't spawn parallel workers that write to the same file — they'll conflict.
- Don't use the correctness judges (`faithfulness-reviewer`, `proof-integrity-reviewer`) for style-only checks — spawn `code-quality-reviewer`, or apply the `code_quality` rubric in the main thread.
- Don't spawn `autoform-reader` for small files (< 100 lines) — just read them directly.
- Don't expect workers to coordinate with each other — they're independent. Cross-cutting concerns (shared namespaces, import organization) are the main thread's job.

## Maximizing parallelism

1. **Extract targets in Setup** and identify the dependency graph.
2. **Separate independent clusters** — targets that share no definitions can be parallelized.
3. **One file per target** — avoid merge conflicts by giving each worker its own output file.
4. **Batch reviews** — after a wave completes, review all files in parallel.
5. **Feed rejection feedback forward** — when a review rejects, include the exact feedback in the retry worker's prompt.
