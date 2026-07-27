# Usage guide

Autoform runs the same durable formalization workflow from Claude Code or
Codex. The host handles interactive planning, review, and escalation through
native subagents; a deterministic dispatcher owns proof workers, jury runs,
queue transitions, and persisted verdicts.

## 1. Install prerequisites

Install the plugin through the host, then run the `install-autoform` skill. It
checks `uv`, Python dependencies, Lean/Mathlib, and optional Zulip access.

For repository development:

```bash
uv sync --extra dev --extra repl --extra zulip
uv run python -m pytest -q
python3 scripts/lint_plugin.py
```

## 2. Create or resume a project

Run the `setup` skill with the source material, desired chapters, Lean project
path, and—if separate—the directory that should own `graph.json`.

Setup:

- creates a Lean/Mathlib project when needed;
- records `metadata.lean_root` in `graph.json`;
- installs namespaced Codex role TOMLs into `.codex/agents/`;
- builds or resumes the source-grounded dependency graph;
- exports the blueprint; and
- starts the loopback-only review dashboard.

Setup never discards an existing plan. “Rebuild” means re-render and resume.
Reset requires an explicit, separately confirmed `--reset-plan`; Autoform first
snapshots the graph, prose, queue, reviews, and activity under
`.autoform/snapshots/`.

Codex discovers project custom agents when a project-rooted task starts. If the
current task predates installation or its spawn tool has no role selector,
Autoform uses a generic native Codex subagent with the complete canonical
`agents/<role>.md` prompt inlined. Open a new task rooted and trusted in the
project to pick up installed roles naturally.

## 3. Select a prover

Run `set-backend` to inspect or persist one of:

| Selection | Prover adapter | Billing/auth path |
|---|---|---|
| `max` | Claude CLI | Claude Max login |
| `codex` | Codex CLI | Codex/OpenAI login |
| `aristotle` | Aristotle API | `ARISTOTLE_API_KEY` |
| `openai` | OpenAI-compatible API | configured key variable |
| `avocado` | explicitly configured Meta-compatible deployment | configured key variable |

With no persisted selection, orchestration defaults to the current interactive
host (`codex` in Codex, `max` in Claude). Persisted choices always win: selecting
`max` while running Codex still requires a working Claude CLI.

OpenAI/Avocado URLs and model IDs are configuration, not guesses. Check them
without sending project data:

```bash
uv run python scripts/provider_check.py <openai|avocado>
```

`--live` is an optional temporary-marker tool-call probe. A successful probe is
not consent for a real workload.

## 4. Orchestrate

Run the `orchestrate` skill. It launches or reuses one dispatcher for the plan,
then continuously drains interactive-host tasks:

| Queue kind | Owner |
|---|---|
| `worker` | deterministic engine + selected prover |
| `reviewer` | deterministic engine + selected judge |
| `planner`, `mathcheck`, graph/content/holistic review | native host subagents |
| `escalation` | interactive host |

Prover and judge backends are independent. For example, Avocado may prove while
Codex judges. When either side uses an API provider, orchestration checks every
distinct provider and obtains explicit project/run data-egress approval before
sending source excerpts, project files, Lean code, tool output, or jury prompts.
The dispatcher enforces this with a repeatable
`--allow-api-egress <openai|avocado>` flag, so a saved backend selection cannot
silently authorize a later process.

## 5. Trust and review

A model’s “proved” status is only a claim. The shared gate:

1. attributes changed Lean files to the run;
2. rejects provider-written elaboration-time execution directives;
3. runs a clean Lean build;
4. enumerates touched declarations;
5. rejects `sorryAx`; and
6. permits only Lean’s standard axioms or explicitly ledgered project axioms.

API writes from any run that is not accepted—including an honest failure or a
rejected proof claim—are restored. Jury failures and malformed outputs abstain;
they never synthesize a passing score.

Use the `review` skill for a text packet or the local dashboard. Human verdicts
are immutable and override AI verdicts.

## Provider environment

Generic OpenAI-compatible settings:

```text
AUTOFORM_OPENAI_BASE_URL
AUTOFORM_OPENAI_MODEL
AUTOFORM_OPENAI_KEY_VAR
AUTOFORM_OPENAI_EXTRA_HEADERS
```

Provider-specific settings use the same suffix, for example
`AUTOFORM_AVOCADO_BASE_URL`. The key setting names an environment variable; the
secret itself is never written to prompts, queues, ledgers, or logs.

Other common settings:

```text
LEAN_PROJECT_DIR
AUTOFORM_CONFIG
AUTOFORM_JUDGE_BACKEND
ARISTOTLE_API_KEY
ZULIPRC
```

`AUTOFORM_UNSAFE_FULL_ACCESS=1` is an explicit escape hatch for dangerous host
permission bypasses. It is unnecessary for normal operation and is never the
default.

For the detailed contracts and compatibility boundary, see
[full-parity-architecture.md](full-parity-architecture.md). For release
validation, disposable live pilots, and failure drills, see
[pilot-testing.md](pilot-testing.md).
