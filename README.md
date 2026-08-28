# AutoformBot

AutoformBot is a Lean 4 plugin that helps AI agents create and maintain
repositories for formalizing existing mathematics. Each repository combines
checked Lean code with a human-readable blueprint: a roadmap, coverage goals,
source notes, and a dependency graph of definitions and theorems.

The plugin provides the minimal guidance experts typically give to keep a
formalization on track, together with orchestration, visualization, execution,
and search tools. People and agents can coordinate work, see what is planned,
ready, blocked, or proved, and publish the blueprint with GitHub Pages.

## Install as a plugin

Install AutoformBot in your favorite agent. The repository includes plugin
manifests for Claude Code, Codex, and Muse Spark. The commands below cover
Claude Code and Codex.

To install AutoformBot directly from GitHub in Claude Code:

```text
/plugin marketplace add facebookresearch/autoform-bot
/plugin install autoform@autoform
```

To install AutoformBot directly from GitHub in Codex:

```bash
codex plugin marketplace add facebookresearch/autoform-bot
codex plugin add autoform@autoform
```

Start a new agent session after installing the plugin so its skills
and MCP servers are reloaded. The installed package and commands retain the
`autoform` identifier; AutoformBot is the project name.

## Plugin surface

Invoke a skill from your coding agent with `/autoform:setup`,
`/autoform:roadmap`, `/autoform:orchestrate`, `/autoform:worker`,
`/autoform:human-review`, `/autoform:agent-review`, or
`/autoform:develop-plugin`.

- `setup` creates or repairs the Lean repository, Markdown blueprint, CI, and Pages infrastructure.
- `roadmap` confirms sources and scope, then builds milestones, coverage, and pull-request-sized DAG nodes.
- `orchestrate` works ready nodes with native subagents and Lean tools.
- `worker` adds the completion and safety contract for a supervisor-managed unattended orchestration turn.
- `human-review` prepares graph, progress, source, and Lean-code views for a person's judgment.
- `agent-review` independently scores roadmap quality or Lean faithfulness, integrity, and code quality from evidence.
- `develop-plugin` maintains Autoform itself through its executable formalization example.

The plugin also gives coding agents execution and inspection tools through a
Lean REPL and LSP. Loogle and semantic search integrations are planned but not
yet implemented.

## The blueprint

Each Lean project keeps its planning material beside its Lean source. The
recommended layout is a portable Markdown directory structure rather than a
generated database:

```text
blueprint/
├── README.md                 project landing page
├── roadmap/                  milestones and pull-request-sized DAG nodes
│   ├── README.md             high-level direction
│   └── convexity/
│       ├── README.md         milestone narrative
│       ├── convex.md         kind: node
│       └── separating-hyperplane.md
├── coverage/                 project-defined completion targets
└── sources/                  optional mathematical source notes
```

Markdown pages under `blueprint/roadmap/` with `kind: node` form a dependency
graph through standard relative links. Optional `declaration` metadata records
the intended Lean artifact without overloading the page kind. The Markdown
remains the source of truth and can be browsed with tools such as Obsidian,
rendered with MkDocs, or published with GitHub Pages. See the [blueprint format
and validation reference](autoform_cli/README.md), the [repository
example](skills/setup/assets/cabannes-thesis-project/README.md), or the [roadmap
example](skills/roadmap/references/cabannes-thesis-roadmap.md).

Conventionally, each first-level roadmap folder is a book chapter and each
node page is one PR-sized major result or important definition. Project,
chapter, one-hop, and full-DAG views are generated from those same files.

## CLI commands

Autoform ships command-line tools with the plugin package. From this
repository, run them through `uv`; if the package is installed in the active
environment, omit `uv run`.

Validate the structure and dependencies of a blueprint, and check that every
declaration a node claims really exists in the Lean sources:

```bash
uv run autoform check blueprint --lean-root .
```

Write the Mermaid dependency graph into the vault, where Obsidian renders it
inline:

```bash
uv run autoform-visualize blueprint
```

Build the publishable blueprint — a book-like overview, aggregate progress,
numbered statement boxes, direct Lean source links, and dependency maps at
project, chapter, theorem-neighborhood, and full-DAG scales:

```bash
uv run autoform render blueprint \
  --output site-src --lean-root . --require-declarations
```

`render` never writes into the vault; point `mkdocs.yml` at its output.

### Unattended Linux worker

On Linux, one idempotent command can keep a Codex orchestration session making
progress. Create `.aiworker` at the formalization repository root and ignore it
locally with `.git/info/exclude` so machine-local objectives cannot be
committed:

```toml
version = 1
objective = "Continue the approved Autoform roadmap."
stale_after_minutes = 15
tool_stale_after_minutes = 60
max_restarts_per_hour = 3
```

Run the same command manually or directly from cron. Use absolute executable
and repository paths because cron normally has a minimal `PATH`:

```cron
* * * * * /absolute/path/to/autoform worker /absolute/path/to/repository >> /tmp/autoform-worker.log 2>&1
```

Each invocation exits quickly. It starts a detached Codex turn when none is
running, leaves a healthy turn alone, resumes a completed session whose result
is `continue`, and stops on `complete`, `blocked`, or `needs_input`. A process
with no structured progress is restarted only after two cron observations;
restart backoff and an hourly circuit breaker prevent retry loops. Private PID,
session, JSONL, and result state lives under
`$XDG_STATE_HOME/autoform/workers/`, or `~/.local/state/autoform/workers/` when
`XDG_STATE_HOME` is unset. Set `AUTOFORM_CODEX_BIN` to an absolute Codex path
when it is not available on cron's `PATH`. Removing `.aiworker` safely stops
and disables an existing worker; recreating it starts a new session, so no
separate stop or start command is needed. Worker turns may read external
sources but never push, merge, publish, post, or message people.

Node status is asserted only where it was checked — statement formalized, proof
formalized, upstreamed, not ready — and everything else is derived from the
graph on every run. A theorem whose own proof compiles but which rests on an
unproved lemma is coloured `proved`, not `fully proved`. The state names and
palette follow [leanblueprint](https://pypi.org/project/leanblueprint/), so the
published site reads like the Lean community's LaTeX blueprints while the
Markdown stays the source of truth. Generated graphs and sites are derived
views and can be rebuilt at any time.

## Development

Invoke `/autoform:develop-plugin` when changing this plugin. It treats the bundled
Cabannes thesis repository as a representative consumer and validates changes
against the results an installed plugin must produce in independent
formalization projects.

Clone the repository and install its development dependencies:

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
make setup
```

Run the test suite with:

```bash
make test
```

To load the checkout in Claude Code, add the repository as a local marketplace:

```bash
claude plugin marketplace add "$(pwd)"
claude plugin install autoform@autoform
```

For Codex, add the local marketplace containing the checkout and install the
local plugin:

```bash
codex plugin marketplace add /path/to/marketplace-root
codex plugin add autoform@autoform-local
```

The marketplace should expose the checkout as `plugins/autoform` and identify
it as `autoform-local`. After changing the plugin, refresh its cache version,
reinstall it, and start a new Codex session:

```bash
python3 /path/to/plugin-creator/scripts/update_plugin_cachebuster.py "$(pwd)"
codex plugin add autoform@autoform-local
```

Agent-facing Lean server architecture and runtime operations are documented in
[`servers/README.md`](servers/README.md).

## Repository layout

```text
.claude-plugin/  Claude Code manifest
.codex-plugin/   Codex manifest
skills/          expert skills, references, and workflow examples
autoform_cli/    command-line interface for blueprint validation and visualization
servers/         public MCP adapters plus the shared Lean runtime
tests/           plugin test cases
```
