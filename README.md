# Autoform

Autoform is a small Lean 4 plugin: three expert skills, a Markdown dependency
graph, and exactly two public MCP servers—LSP and REPL—backed by one shared,
node-local Lean runtime. Planning stays readable and editable by people while
the coding agent supplies the orchestration.

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
repo-shaped [`examples/`](examples/README.md) directory for the complete vault,
site configuration, and workflow. Roadmap and coverage organization remains
project policy; Autoform deliberately enforces only the fine-grained node DAG.

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
builders configured to emit `.html` pages, as in the bundled MkDocs example.

## Plugin surface

- `setup` creates or repairs the Markdown blueprint after confirming sources and scope.
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
runtime for the current Autoform installation, Unix user, and compute node.
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
skills/         three short expert nudges with on-demand review rubrics
autoform_cli/   blueprint validation and visualization commands
servers/        two public MCP adapters plus the shared Lean runtime
tests/          graph, packaging, and server contracts
```
