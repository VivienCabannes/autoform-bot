# AutoformBot

AutoformBot is a Lean 4 plugin that gives AI agents the minimal guidance
experts typically provide to keep formalization on track. It combines
orchestration and visualization with execution and search tools. Its
human-editable Markdown roadmap and wiki capture the high-level direction and
theorem-sized dependency nodes, so people can refine the plan while agents
coordinate the work.

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

## The blueprint

Each Lean project keeps its planning material beside its Lean source. The
recommended layout is a portable Markdown vault rather than a generated
database:

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

Open `blueprint/` directly as an Obsidian vault: standard relative links power
its backlinks and graph view, while `.obsidian/` remains ignored. The same
Markdown can be rendered by MkDocs and deployed by GitHub Pages. See the
[Setup repository example](skills/setup/assets/cabannes-thesis-project/README.md)
for the complete repository shell and the Roadmap skill's concise
[Cabannes thesis example](skills/roadmap/references/cabannes-thesis-roadmap.md)
for source-to-DAG planning. Roadmap and coverage organization remains project
policy; AutoformBot deliberately enforces only the fine-grained node DAG.

Every Markdown file below `blueprint/nodes/` is one node. Its relative path
without `.md` is its stable ID. The H1 is its human title; optional frontmatter
records lightweight metadata:

```markdown
---
kind: theorem
status: ready
lean: MyProject.separatingHyperplane
---

# Separating hyperplane theorem

State the intended result and proof sketch here.

## Depends on

- [Convex set](../definitions/convex.md)

## Sources

- [Chapter 2](../../sources/convexity.md#separation)
```

Only links under `## Depends on` are graph edges. Other links are ordinary wiki
navigation or citations. Dependency links are resolved relative to the current
node and must point to another file inside `blueprint/nodes/`; cycles, missing
targets, escaping paths, self-links, and missing H1 titles are rejected.

`status` is deliberately lightweight: use `planned`, `ready`, `blocked`, or
`proved`. A node is ready when all linked prerequisites are proved; a proved
node should name its compiled declaration in `lean`. The checker validates graph
structure but leaves mathematical and Lean correctness to the agent and Lean.

The Markdown files are the sole source of truth. Generated graph and site files
are derived, read-only views and may be regenerated at any time.

## Commands

```bash
uv sync --extra dev --extra repl
uv run autoform check blueprint
uv run autoform-visualize blueprint
uv run autoform-visualize blueprint \
  --output blueprint/dependencies.html \
  --link-extension .html
uv run pytest -q
```

The visualization is self-contained and works directly from `file://`; clicking
a node opens its Markdown file. The HTML-link mode is intended for static-site
builders configured to emit `.html` pages, as in the setup skill's MkDocs asset.

## Plugin surface

- `setup` creates or repairs the Lean repository, Markdown vault, CI, and Pages infrastructure.
- `roadmap` confirms sources and scope, then builds roadmap, coverage, and theorem DAG notes.
- `orchestrate` works ready nodes with native subagents and Lean tools.
- `review` independently checks faithfulness, proof integrity, and Mathlib code quality.
- `autoform-lsp` provides Lean diagnostics and hover information.
- `autoform-repl` executes snippets through a persistent Lean process.

The four Lean tools require an absolute `project_dir`, so the plugin directory
is never mistaken for the user's Lean project. Stateless Mathlib or community
search stays outside the server surface and uses host-native tools when useful.

### Shared Lean runtime

Plugin hosts still start the two stdio MCP processes automatically. They are
lightweight adapters: the first Lean tool call race-safely starts a detached
runtime for the current AutoformBot installation, Unix user, and compute node.
That runtime owns one resident REPL pool and LSP session per active Lean
project, so sessions using that installation reuse the same warmed processes.
Closing the session that happened to start it does not stop it; after a crash,
the next tool call starts it again. Runtime sockets include a code fingerprint;
after an in-place upgrade, the next call gracefully replaces the older build
instead of silently reusing stale daemon code.

REPL and LSP processes remain lazy. A cold tool call stays pending while Lean
warms up, so no `/repl-start`, `/lsp-start`, or model-side sleep is needed. Idle
project processes are closed after 30 minutes by default, while the small
runtime remains available. Its lifecycle is also explicit:

```bash
uv run autoform-lean-runtime start
uv run autoform-lean-runtime status
uv run autoform-lean-runtime stop
```

`stop` is graceful: it waits for admitted tool calls and Lean children to
finish shutting down before a subsequent `start` can replace the runtime.

The private socket lives below `$XDG_RUNTIME_DIR/autoform`, falling back to a
uid-specific directory in `/tmp`; the rotating runtime log is beside it.
`AUTOFORM_RUNTIME_DIR` overrides that location. Node-wide limits are controlled
by `AUTOFORM_REPL_TOTAL_WORKERS`, `AUTOFORM_REPL_WORKERS_PER_PROJECT`,
`AUTOFORM_MAX_LEAN_PROJECTS`, and `AUTOFORM_LEAN_IDLE_SECONDS`. The first
process to start the runtime supplies those settings until it is stopped.
`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` can raise the client/daemon response budget
when unusually large worker pools need more than the default 15 minutes to warm.

## Repository layout

```text
.claude-plugin/  Claude Code manifest
.codex-plugin/   Codex manifest
skills/         four expert workflows, references, and a compact thesis project asset
autoform_cli/   blueprint validation and visualization commands
servers/        two public MCP adapters plus the shared Lean runtime
tests/          graph, packaging, and server contracts
```
