---
name: evaluate
description: >-
  Audit Lean formalization statements or benchmark an Autoform prover against
  an existing task corpus. Use for bulk statement QA, faithfulness checks,
  corpus comparison, prover evaluation, regression benchmarks, or measuring a
  backend without changing source tasks. Durable DAG proving and review belong
  to Orchestrate.
---

# Evaluate formalizations and provers

Run offline corpus QA without changing the Autoform roadmap. This workflow has
two modes:

- `audit` checks formalized statements, optionally compiles them, and can run
  the existing structured faithfulness judge.
- `benchmark` runs the existing unified prover and kernel gate against task
  copies in disposable Lean projects.

Do not use this workflow to prove or review nodes in the durable Autoform DAG.
Use Orchestrate for that work.

## Resolve the plugin root

Resolve one absolute plugin root from a valid `AUTOFORM_PLUGIN_ROOT`,
`MUSE_PLUGIN_ROOT`, `PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
`Path(<this loaded SKILL.md>).resolve().parents[2]`. Validate that it contains
`scripts/evaluate.py` and `servers/prover/verify.py`. Replace
`<AUTOFORM_PLUGIN_ROOT>` below with that quoted absolute path.

## Start with a run brief

Before running anything, report:

- mode (`audit` or `benchmark`), target corpus, Lean project root, and case
  count;
- whether models will run, the selected backend and billing/data path, and
  whether project content may leave the machine;
- output paths;
- that audit is read-only and benchmark uses disposable project copies.

Model-backed evaluation requires explicit approval for the run. Direct OpenAI
or Avocado API use separately requires approval for API egress. A persisted
backend choice does not supply either approval.

## Audit statements

The target may be an Autoform `graph.json`, a directory containing legacy task
folders, or one `formalized_statement.lean`. Start with the static, read-only
audit:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/evaluate.py" audit "$TARGET" \
  --project-root "$PROJECT_DIR" --output "$OUTPUT/audit.json"
```

Add `--compile` to check each statement with `lake env lean`. Static checks and
compilation do not invoke a model.

When the user explicitly approves model use, add
`--judge-backend <claude|codex|muse|openai|avocado> --confirm-model-use`.
Before direct OpenAI or Avocado use, report the configured provider and base
URL, obtain API-egress approval, run `scripts/provider_check.py <provider>`, and
also add `--allow-api-egress`.

Report clean, flagged, and rejected counts, then list every major or critical
finding. Do not edit the audited files.

## Benchmark a prover

Benchmark accepts a task corpus inside a Lean project. Each task directory must
contain `formalized_statement.lean`; `natural_language_statement.md` and
`formalized_proof.lean` are optional. State the selected backend and obtain
explicit approval before running:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/evaluate.py" benchmark "$CORPUS" \
  --project-root "$PROJECT_DIR" --output "$OUTPUT" \
  --backend "$BACKEND" --confirm-model-use
```

For direct OpenAI or Avocado, perform the same provider check and approval as
audit, then add `--allow-api-egress`. Use `--limit N` for a smoke test before a
full corpus run. Existing results resume by case ID; add `--force` only when the
user requests a rerun.

The runner copies the Lean project, removes any reference proof only in that
copy, invokes Autoform's unified prover, and requires both the shared kernel
gate and unchanged theorem headers. It never claims equivalence beyond that
verification basis. Do not install or invent a separate comparator. Results
are written to `results.jsonl`, `summary.json`, and `artifacts/*.lean` under
the requested output directory. Verify that the source statement and reference
proof remain unchanged before reporting completion.
