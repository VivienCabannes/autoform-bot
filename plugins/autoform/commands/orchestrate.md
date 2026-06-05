---
description: Run the autoform-bot pipeline over a whole book (the full engine).
argument-hint: "--config FILE --name RUN [--fresh] [--backend native]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, TodoWrite
---

# /autoform:orchestrate — whole-book formalization run

Drive an end-to-end formalization of a whole book: the autoform-bot coordinator plans a granular
task DAG, runs a worker pool, gates results through a merge queue, and evaluates as it goes.
Arguments: `$ARGUMENTS`.

Resolve and **echo**: config file, run name, `--fresh` vs resume, and backend (**default
`python`**). Load **formalization-workflow** and **eval-rubrics**.

## Default — python backend (the autoform-bot coordinator)

This is the primary entry point and runs the real engine (its own DAG planning, worker pool,
bors-style merge queue, merge-eval, traces, optional SLURM multi-node).

1. Preflight: an autoform-bot checkout with deps installed (`uv sync`), a `config.yaml` (see
   `autoform/bot/configs/` for examples — sets workspace, book, targets, model, worker counts),
   and the inference API key(s).
2. Print the command, then run it (or print only with `--dry-run`):

```bash
# fresh run
python -m autoform.bot.main run --config=<config> --name=<run-name> --fresh
# resume an interrupted run (omit --fresh)
python -m autoform.bot.main run --config=<config> --name=<run-name>
# multi-node (SLURM)
srun --nodes=N --ntasks-per-node=1 python -m autoform.bot.main run --config=<config> --name=<run-name>
```

3. Point the user at the visualizer to watch progress:
   `python -m autoform.visualizer.app --runs-dir=<workspace> --port=8003`.

## Opt-in — native backend (`--backend native`)

Runs the round loop in this session (no autoform-bot checkout). Use `TodoWrite` as the DAG store
and the **orchestrator** agent as the planner; takes `<targets.yaml> --book-dir DIR --repo-dir
DIR [--max-parallel N]` instead of `--config`.

**Round 0 — Plan.** Dispatch **orchestrator** to read the book + `targets.yaml` and emit a DAG:
one task per statement, kebab-case type-prefixed IDs (`def-…`, `thm-…`, `fix-…`), dependencies
from the book's logical structure.

**Each round:** dispatch ready tasks (up to `--max-parallel`) via `/autoform:formalize` (the
single-target loop); evaluate merged work with `/autoform:eval --backend native`; re-dispatch
**orchestrator** with the reports to act on every failed goal (improve prompt / split / delete).
Repeat until all targets pass.

## Required artifacts

Python: the resolved command + run name + visualizer URL. Native: a per-round DAG status block,
the formalize gate tables, and the cumulative eval scorecard.
