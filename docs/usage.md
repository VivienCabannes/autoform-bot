# Usage guide

Autoform runs the same durable formalization workflow from Claude Code or
Codex. The host handles interactive planning, review, and escalation through
native subagents; a deterministic dispatcher owns proof workers, jury runs,
queue transitions, and persisted verdicts.

## User command surface

AutoformBot exposes exactly four user workflows: `setup`, `roadmap`,
`orchestrate`, and `evaluate`. Supporting functions such as installation,
workspace inspection, planning, visualization, review, and Zulip search are
internal to those workflows and do not appear as separate commands.

## 1. Install prerequisites

Install the plugin through the host, then run `setup`. It checks `uv`, Python
dependencies, Lean/Mathlib, and optional Zulip access before creating or
resuming a project.

For repository development:

```bash
uv sync --extra dev --extra zulip
uv run python -m pytest -q
python3 scripts/lint_plugin.py
```

## 2. Create or resume a project

Run `setup` with the Lean project path and—if separate—the directory that
should own `graph.json`.

Setup:

- creates a Lean/Mathlib project when needed;
- initializes empty durable state and records `metadata.lean_root` in
  `graph.json`;
- installs namespaced Codex role TOMLs into `.codex/agents/`;
- starts the loopback-only review dashboard; and
- prepares GitHub readiness (CI, Pages, distributed mode) with explicit
  approval for each outward-facing action.

Then run `roadmap` with the source material and desired chapters. Roadmap
confirms sources and scope, builds or resumes the source-grounded dependency
graph (coarse clusters → user approval → detailed split/check/review waves),
and renders the blueprint on request.

Roadmap never discards an existing plan. “Rebuild” means re-render and resume.
Reset requires an explicit, separately confirmed `--reset-plan`; AutoformBot
first snapshots the graph, prose, queue, reviews, and activity under
`.autoform/snapshots/`.

Codex discovers project custom agents when a project-rooted task starts. If the
current task predates installation or its spawn tool has no role selector,
Autoform uses a generic native Codex subagent with the complete canonical
`agents/<role>.md` prompt inlined. Open a new task rooted and trusted in the
project to pick up installed roles naturally.

## 3. Select a prover in Orchestrate

Ask `orchestrate` to inspect the available backends, select one for a run, or
persist one as the default:

| Selection | User-facing backend | Execution path | Billing/auth path |
|---|---|---|---|
| `max` | Claude Code (Max subscription) | Claude CLI | Claude Max login; API credentials disabled |
| `codex` | Codex | Codex CLI | Codex/OpenAI login |
| `aristotle` | Aristotle | Aristotle API | `ARISTOTLE_API_KEY` |
| `muse` | Muse/TBH | TBH CLI | configured Meta provider/authentication |
| `openai` | Custom API (OpenAI-compatible) | direct Chat Completions requests with Autoform's bounded tool loop | configured key variable |
| `avocado` | Meta Avocado | explicitly configured Meta-compatible deployment | configured key variable |

An explicit choice for the current run overrides the persisted default without
changing it. With no explicit or persisted selection, orchestration defaults to
the current interactive host (`codex` in Codex, `max` in Claude, `muse` in
Muse). Selecting `max` while running another host still requires a working
Claude CLI.

The custom API backend is not Codex: it bypasses the Codex CLI and sends direct
Chat Completions requests to the configured provider. OpenAI/Avocado URLs and
model IDs are configuration, not guesses. Check them without sending project
data:

```bash
uv run python scripts/provider_check.py <openai|avocado>
```

`--live` is an optional temporary-marker tool-call probe. A successful probe is
not consent for a real workload.

## 4. Orchestrate

Run `orchestrate`. It launches or reuses one dispatcher for the plan,
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

Ask Orchestrate for a text review packet or the local dashboard. Human
verdicts are immutable and override AI verdicts.

## 6. Evaluate a corpus or prover

Run `evaluate` for work outside the durable roadmap queue:

- `audit` performs read-only static statement checks, optional Lean
  compilation, and an optional structured faithfulness judgment;
- `benchmark` copies each task into a disposable Lean project, runs the unified
  prover and kernel gate, and rejects changed theorem headers.

Both model-backed modes require explicit approval. Direct OpenAI or Avocado
use also requires explicit API-egress approval. Benchmark outputs results,
summaries, and proof artifacts under a caller-selected directory; it never
blanks or edits the source corpus.

## 7. Publish a read-only snapshot

The local dashboard remains the only operational surface. GitHub Pages is an
optional snapshot of committed graph structure, theorem content, proof status,
review verdicts, and kernel evidence. It excludes agent activity, queues, logs,
backend/provider settings, credentials, reviewer notes and identities, and local
paths.

Run `setup` and explicitly request GitHub Pages publication. Setup first inspects
the Git remote and repository visibility, then shows the exact publication
contract. It writes `.autoform/pages.json` and
`.github/workflows/autoform-pages.yml` only after approval. Private or internal
repositories require separate verification that the account or enterprise plan
provides the intended Pages access control.

The generated workflow pins GitHub actions and the Autoform exporter to commit
hashes. It deploys only after durable dashboard inputs are committed and pushed;
no local Python server or plugin cache is required by the exported site.

## Provider environment

Custom API (OpenAI-compatible) settings:

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
