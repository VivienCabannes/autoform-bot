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
manifests for Claude Code, Codex, and Muse Spark; the commands below use Claude
Code as a concrete example, with similar marketplace and install commands
available in Codex and Muse Spark.

To install AutoformBot directly from GitHub in Claude Code:

```text
/plugin marketplace add facebookresearch/autoform-bot
/plugin install autoform@autoform
```

To install a local checkout instead, clone the repository and add its directory
as a Claude Code marketplace:

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
claude plugin marketplace add "$(pwd)"
claude plugin install autoform@autoform
```

To install AutoformBot directly from GitHub in Codex:

```bash
codex plugin marketplace add facebookresearch/autoform-bot
codex plugin add autoform@autoform
```

To install a local checkout in Codex instead, add the local marketplace
containing the checkout and install AutoformBot from it:

```bash
codex plugin marketplace add /path/to/marketplace-root
codex plugin add autoform@autoform-local
```

The marketplace should expose the checkout as `plugins/autoform` and identify
it as `autoform-local`. After changing the plugin during development, refresh
its cache version and reinstall it:

```bash
python3 /path/to/plugin-creator/scripts/update_plugin_cachebuster.py "$(pwd)"
codex plugin add autoform@autoform-local
```

Start a new agent session after installing or updating the plugin so its skills
and MCP servers are reloaded. The installed package and commands retain the
`autoform` identifier; AutoformBot is the project name.

For development, install the repository's Python dependencies separately:

```bash
make setup
```

## Plugin surface

Invoke a skill from your coding agent with `/autoform:setup`,
`/autoform:roadmap`, `/autoform:orchestrate`, or `/autoform:review`.

- `setup` creates or repairs the Lean repository, Markdown blueprint, CI, and Pages infrastructure.
- `roadmap` confirms sources and scope, then builds roadmap, coverage, and theorem DAG notes.
- `orchestrate` works ready nodes with native subagents and Lean tools.
- `review` independently checks faithfulness, proof integrity, and Mathlib code quality.

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
├── roadmap/                  high-level direction and milestones
├── coverage/                 project-defined completion targets
├── sources/                  optional mathematical source notes
└── nodes/                    theorem-sized executable DAG
    ├── definitions/
    │   └── convex.md
    └── theorems/
        └── separating-hyperplane.md
```

Files under `blueprint/nodes/` form a dependency graph through standard relative
links. The Markdown remains the source of truth and can be browsed with tools
such as Obsidian, rendered with MkDocs, or published with GitHub Pages. See the
[blueprint format and validation reference](autoform_cli/README.md), the
[repository example](skills/setup/assets/cabannes-thesis-project/README.md), or
the [roadmap example](skills/roadmap/references/cabannes-thesis-roadmap.md).

## CLI commands

Autoform ships command-line tools with the plugin package. From this
repository, run them through `uv`; if the package is installed in the active
environment, omit `uv run`.

Validate the structure and dependencies of a blueprint:

```bash
uv run autoform check blueprint
```

Generate a self-contained HTML visualization whose nodes link to their Markdown
files:

```bash
uv run autoform-visualize blueprint
```

For MkDocs or GitHub Pages, generate `.html` links instead:

```bash
uv run autoform-visualize blueprint \
  --output blueprint/dependencies.html \
  --link-extension .html
```

The local visualization works directly from `file://`. Generated graphs are
derived views and can be rebuilt at any time.

## Development

Install development dependencies and run the tests from a repository checkout:

```bash
uv sync --extra dev --extra repl
uv run pytest -q
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
