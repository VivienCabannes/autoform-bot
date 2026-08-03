# Autoform

**A host-neutral autoformalization engine for Claude Code, Codex, and Muse: turn a mathematics textbook into
kernel-verified Lean 4, with every proof independently checked and every verdict
auditable.**

Autoform is an Agent Skills plugin that runs the full pipeline on Claude Code,
Codex, or Muse — plan a textbook
into a tiered dependency graph, prove nodes with swappable AI backends, verify
every claimed proof against the Lean kernel, judge the result with a three-axis
review jury, and put a human in charge of final sign-off through a local review
dashboard. Its central design commitment: **no model is ever trusted about its
own proof.** A backend's "proved" is only a claim until an independent gate
rebuilds the project and audits its axioms (`sorryAx` and non-standard axioms
are rejected), so adding a new — even unknown — model backend is safe by
construction.

The shared Codex paths are implemented and covered by deterministic tests, but
operational parity remains a release gate: host authentication, project trust,
generated-role discovery, CLI schema behavior, and one real proof/jury run must
pass on the release candidate. See
[Codex implementation and release status](docs/codex-support.md); automated
tests alone are not presented as end-to-end live validation.

> **Version 2 transition:** this plugin intentionally replaces the standalone
> v1 Python research pipeline; it is not an in-place compatibility upgrade.
> Existing v1 runs and Python integrations should remain pinned to an archival
> v1 revision. See [the v2 migration guide](docs/migration-v2.md) for the
> removed-capability map, migration procedure, and rollback boundary.

## How it works

```mermaid
flowchart LR
    A[textbook<br/>LaTeX / MD / PDF] -->|/autoform:setup| B[tiered dependency graph<br/>graph.json + prose per node]
    B -->|/autoform:orchestrate| C[prover workers<br/>claude · aristotle · codex · muse · openai · avocado]
    C --> D{honesty gate<br/>lake build + #print axioms}
    D -->|rejected| C
    D -->|verified| E[3-judge review jury<br/>faithfulness · proof integrity · style]
    E --> F[review dashboard<br/>human sign-off]
    B -.-> G[interactive blueprint]
    H["/autoform:set-backend"] -.-> C
```

- **Plan** — a two-phase pipeline reads your sources and builds a tiered DAG:
  coarse concept clusters first, then fine per-statement nodes with their own
  paraphrased prose, each mapped against Mathlib (in-mathlib / partial /
  missing). Rendered as an interactive leanblueprint with a tier toggle.
- **Prove** — a deterministic dispatch engine drains a shared task queue:
  prover workers write real Lean, iterating against build feedback. Backends
  are thin adapters behind one driver; a steering judge watches live-steerable
  backends and folds the gate's own rejection reason back as a corrective turn
  for the rest.
- **Verify** — every "proved" passes the honesty gate: the project builds
  clean, the touched declarations' axioms are audited (`#print axioms`), and
  anything beyond Lean's standard axioms — or any `sorry` — downgrades the
  claim to failed.
- **Review** — three single-axis judges score each node (faithfulness 0.40,
  proof integrity 0.40, code quality 0.20); thresholds gate a
  clean / flagged / rejected verdict. Humans review packets or use the local
  dashboard; a human verdict is immutable and always wins over the AI's.

## Quickstart

Prereqs: [Claude Code](https://claude.com/claude-code),
[Codex](https://developers.openai.com/codex/), or Muse/TBH; Python ≥ 3.10; and
[uv](https://docs.astral.sh/uv/) (the MCP servers launch via `uv run` and
resolve their own deps on first start).

Claude Code:

```text
/plugin marketplace add VivienCabannes/autoform-bot
/plugin install autoform@autoform
```

Codex, from a checkout of this repository:

```bash
make install-codex
```

Muse/TBH, from a checkout of this repository:

```bash
make build-muse
tbh plugins validate dist/muse/autoform --json
tbh plugins install dist/muse/autoform
tbh plugins enable autoform
```

Approve Autoform's hook and MCP capabilities in `/plugins` under the Runtime
tab. Muse can orchestrate the existing backends or run proofs and jury reviews
itself through the `muse` backend.

Start a new task after installing or upgrading so the host reloads the plugin,
then use:

```text
/autoform:setup                 # new Lean+Mathlib project → plan → blueprint → dashboard
/autoform:orchestrate           # launch the engine: prover workers + review jury
/autoform:set-backend           # choose the prover backend and billing/data path
```

`/autoform:setup` walks you through creating a project (via the LeanProject
template, with Mathlib cache), repairing prerequisites, inspecting an existing
workspace, planning your sources into `graph.json`, and opening the review
dashboard. `/autoform:set-backend` persists the default
prover backend (`max` | `aristotle` | `codex` | `muse` | `openai` | `avocado`);
`/autoform:orchestrate` then drives the
formalization — autonomously, human-driven from the dashboard, or both, off one
shared queue.

## Prover backends

One MCP tool proves a node — `prove_node(graph_path, node_id, project_dir,
backend=...)` — and the driver, steerer, and honesty gate are identical for
every backend; only the adapter differs. Direct OpenAI/Avocado calls also
require `allow_api_egress=true` after explicit approval for that project/run.

| backend | what it is | auth / env | steering |
|---|---|---|---|
| `max` | **Claude Code (Max subscription)** — headless Claude Code worker | your Claude login (API credentials disabled → subscription billing) | gate-fold (live judge opt-in) |
| `aristotle` | Harmonic's Aristotle prover API | `ARISTOTLE_API_KEY` + `uv sync --extra aristotle` | **in-flight** (`project.ask`) |
| `codex` | OpenAI Codex CLI | codex's own auth | gate-fold (live judge opt-in) |
| `muse` | Muse/TBH CLI | configured Meta provider/authentication | one sandboxed run (no headless resume) |
| `openai` | **Custom API (OpenAI-compatible)** — direct Chat Completions endpoint, not the Codex CLI | `AUTOFORM_OPENAI_BASE_URL` / `_MODEL` / `_KEY_VAR` | bounded local tool loop |
| `avocado` | explicitly configured Meta-compatible deployment | configured key variable via `AUTOFORM_AVOCADO_*` | bounded local tool loop |

Muse worker settings can be overridden with `AUTOFORM_MUSE_BIN`,
`AUTOFORM_MUSE_PROVIDER`, `AUTOFORM_MUSE_MODEL`, `AUTOFORM_MUSE_PRESET`,
`AUTOFORM_MUSE_REASONING_EFFORT`, `AUTOFORM_MUSE_MAX_MODEL_STEPS`, and
`AUTOFORM_MUSE_RUNTIME_DIR`.

Check API configuration without sending project data with
`uv run python scripts/provider_check.py <openai|avocado>`. Add `--live` only to run a
temporary-marker tool-call probe.

Steering is capability-aware: Aristotle accepts corrections mid-run; resumable
Claude and Codex CLIs get the deterministic **verify-gate fold** (the gate's
rejection reason, fed back verbatim as one corrective turn); Muse and
request/response backends retry at the dispatch layer. The steering judge runs
on structured signals, not a timer.

## Self-reporting: formalization.yaml + usage ledger

Projects created by autoform carry a
[mathlib-initiative `formalization.yaml`](https://github.com/mathlib-initiative/formalization.yaml)
manifest (v0.3). Every prover run appends token/cost/wall-time telemetry to an
append-only ledger (`.autoform/usage.jsonl`) and refreshes the manifest's
machine-owned fields — models used, wall time, spend, token totals, sorry
counts — while everything human-written round-trips untouched. Retrofit an
existing project with `python3 scripts/formalization.py init <project-dir>`.

## The surface

**The complete user command surface** — `/autoform:setup` (installation,
inspection, planning, visualization, and project setup),
`/autoform:orchestrate` (launch/drive the engine),
`/autoform:set-backend` (persist the prover backend; shared with the
dashboard).

Planning, visualization, review, Mathlib conventions, proof discipline,
environment repair, workspace inspection, jury rubrics, and Zulip search are
internal runbooks or MCP capabilities invoked by Setup and Orchestrate. They do
not appear as extra slash commands.

**Agents** — a prover `autoform-worker` and an `autoform-reader`; the planning
crew (`splitter`, `mathlib-checker`, `graph-reviewer`, `content-reviewer`,
`holistic-reviewer`, `source-searcher`); and the review jury
(`faithfulness-reviewer`, `proof-integrity-reviewer`, `code-quality-reviewer`).

**MCP servers** — `autoform-prover` (the unified `prove_node`),
`autoform-aristotle` (session-level Aristotle access),
`lean-informal-planner-mathlib` (ripgrep-backed Mathlib search),
`autoform-zulip` (Zulip search; needs a `.zuliprc`), and `autoform-repl` /
`autoform-lsp` (**stubs today** — reference implementations live in
`examples/servers/`; a real pooled REPL is landing separately).

## Repository layout

```
skills/       exactly three user workflows: setup, orchestrate, set-backend
internal/     non-discoverable runbooks, reference material, and jury rubrics
agents/       worker, reader, planning crew, review jury
servers/      MCP servers (prover, aristotle, mathlib, zulip; repl/lsp stubs)
scripts/      plan/graph tooling, dispatch engine, review UI, formalization.py
hooks/        Claude SessionStart context (skills are the workflow surface)
docs/         pipeline architecture, usage guide, backend handoff notes
examples/     reference implementations for the remaining stubs
tests/        deterministic suite; optional local-Lean and loopback-HTTP smoke tests
```

## Development

```bash
uv sync --extra dev --extra repl --extra zulip
uv run python -m pytest -q               # full suite
python3 scripts/lint_plugin.py           # plugin-surface lint (CI runs this)
uv run ruff check scripts servers tests skills
make demo PYTHON="uv run python"
```

CI runs the deterministic suite and bundled demo on Python 3.10–3.14, plus
Python and plugin-surface lint. These checks validate shared contracts without
paid credentials; they do not establish that a particular host login, CLI
version, or provider account works. See [CONTRIBUTING.md](CONTRIBUTING.md) for
the component status table, [docs/pipeline.md](docs/pipeline.md) for the
planner's architecture, and [docs/pilot-testing.md](docs/pilot-testing.md) for
the required live-host and failure-drill release checklist.

## License

MIT © Meta Platforms, Inc. and affiliates.
