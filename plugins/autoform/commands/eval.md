---
description: Grade Lean declarations — rubric scores plus an axiom check.
argument-hint: "--repo-dir DIR --task-file FILE --book-dir DIR [--backend native]"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /autoform:eval — rubric evaluation

Grade formalized targets against the book using autoform-bot's jury rubrics and `#print axioms`
checks. Arguments: `$ARGUMENTS`.

Resolve and **echo**: repo dir, task file (`targets.yaml`), book dir, and backend (**default
`python`**). Load the **eval-rubrics** skill.

## Default — python backend (the autoform-bot evaluator)

By default this runs autoform-bot's evaluator. Preflight (autoform-bot checkout + deps + API
key), print the command, then run it (or print only with `--dry-run`):

```bash
python -m autoform.eval run \
    --repo-dir=<repo-dir> \
    --task-file=<task-file> \
    --book-dir=<book-dir>
```

Report where `report.json` / `report.md` landed and summarize pass/fail counts.

## Opt-in — native backend (`--backend native`)

Runs the rubric jury in this session — no autoform-bot checkout needed, useful for a quick scan
of a few declarations.

**Phase 1 — Repo gate.** `lake build` the relevant targets (must pass); scan for forbidden
metaprogramming (`elab`/`macro`/`syntax`) and stray `sorry`/`axiom`. A failed gate fails
everything downstream with score 0.

**Phase 2 — Per statement (fan-out).** For each target:
1. **matcher** subagent → the Lean declaration (namespace-qualified) or `not_found`.
2. `#print axioms <decl>` via `lake env lean` → flag anything beyond `propext`,
   `Classical.choice`, `Quot.sound`, and any `sorryAx`.
3. A **judge** subagent per active rubric (faithfulness, proof_integrity, code_quality; correctness/
   style as configured) → strict `{score, reasoning}`.

**Phase 3 — Aggregate.** Weighted mean; a target **passes** only if every active rubric clears
its threshold (see eval-rubrics). Emit a scorecard: per-target match, per-rubric score + reason,
axioms found, pass/fail, actionable fixes.

## Next

Feed failures back into `/autoform:formalize` / `/autoform:orchestrate` (one fix per failing
target or `sorry`).
