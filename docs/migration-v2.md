# Migrating from the v1 pipeline to the v2 plugin

## Decision

Version 2 is an intentional product replacement. It does not preserve the v1
Python application's public modules or command-line entry points inside the new
plugin.

The v1 repository was a standalone, configuration-driven research pipeline.
It included statement extraction, multi-node orchestration, dataset evaluation,
trace visualization, a generic inference framework, and reusable execution
tools. Version 2 is an Agent Skills plugin whose durable control plane is
`graph.json`, the dispatch queue, the review sidecar, and the unified prover and
verification contracts.

Keeping both implementations in one package would create two orchestration
models, two provider abstractions, and two incompatible run formats. The v1
code should therefore remain available through repository history or an
archival release rather than as a compatibility layer in v2.

Before merging v2 upstream, maintainers should create an archival tag or branch
at the current upstream `main` commit. The v2 merge is reversible through Git,
but its runtime artifacts are not automatically convertible back to v1.

## Capability map

| v1 surface | v2 status | Migration path |
|---|---|---|
| `python -m autoform.statement_extraction` | Removed | Put source material in the project and run `setup`, which owns planning. Review the resulting `graph.json`; no automatic `targets.yaml` conversion is provided. |
| `python -m autoform.bot.main` and YAML run configuration | Removed | Run the host-native `setup` and `orchestrate` workflows. Backend selection moves to `set-backend`; durable state lives beside the Lean project. |
| Multi-node and SLURM coordination | Removed | The v2 dispatcher supports bounded local concurrency. Keep v1 for existing cluster runs until a separate distributed dispatcher is designed. |
| `python -m autoform.eval` dataset evaluation | Removed | Use the node-level three-axis jury and review dashboard. Dataset metrics and historical evaluator outputs have no v2 importer. |
| `python -m autoform.visualizer.app` run/trace dashboard | Removed | Use the DAG review dashboard for current plans. Historical v1 traces remain viewable only with a v1 checkout. |
| `core.inference` and `InferenceProtocol` | Removed | Implement a v2 `ProverAdapter`, or configure the bounded OpenAI-compatible adapter. Gemini, Ollama, and vLLM do not have v2 adapters today. |
| Generic `core.mcp` and `tools/` packages | Removed | Use the plugin's stateful REPL and LSP servers; stateless operations remain native workflow steps. |
| `autoform/data/<book>` run layout | Removed | Keep sources and the generated graph/review artifacts with the target Lean project. Existing run directories are not modified by v2. |
| Imports from `autoform`, `core`, or `tools` | Incompatible | Pin a v1 checkout for dependent Python code. Version 2 packages the plugin server surface, not the former application libraries. |

## Migration procedure

1. Record the exact v1 commit and archive the v1 run directory, configuration,
   extracted targets, traces, evaluation reports, and Lean workspaces.
2. Keep the archived run immutable. Do not point v2 at it as though it were a
   resumable v2 plan.
3. Install v2 in Claude Code or Codex and open a separate Lean project.
4. Copy only the mathematical sources and any Lean code that should be reused.
5. Run `setup` / `plan`, review the new dependency graph, and explicitly map
   important v1 targets that were not recovered from the sources.
6. Select and preflight the prover and jury backends, then run the disposable
   pilot in [pilot-testing.md](pilot-testing.md) before starting a long job.
7. Keep the v1 environment available until the v2 graph, proofs, review
   sidecar, and usage ledger have been independently accepted.

There is intentionally no automatic migration for v1 agent traces, worktree
state, evaluator databases, or provider conversations. Those formats encode a
different execution model, and silently approximating them would make resume
and audit claims unreliable.

## Rollback

Stop all v2 dispatchers, preserve the Lean project and its `.autoform` artifacts,
and return to the archived v1 tag or branch. A rollback does not translate v2
queue, review, or usage records into v1 records.

If coexistence becomes a requirement, it should be delivered as two versioned
packages or repositories joined by an explicit artifact converter—not by
restoring the deleted v1 application underneath the v2 plugin.
