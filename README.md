# Autoform

Autoform is a small Lean 4 plugin: three expert skills, a Markdown dependency
graph, and exactly two stateful MCP servers—LSP and REPL. Planning stays
readable and editable by people while the coding agent supplies the
orchestration.

## The blueprint

Each Lean project keeps its graph in a committed wiki:

```text
blueprint/
├── README.md
└── nodes/
    ├── definitions/
    │   └── convex.md
    └── theorems/
        └── separating-hyperplane.md
```

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

The Markdown files are the sole source of truth. `graph.html` is a derived,
read-only visualization and may be regenerated at any time.

## Commands

```bash
uv sync --extra dev --extra repl
uv run autoform check blueprint
uv run autoform-visualize blueprint
uv run pytest -q
```

The visualization is self-contained and works directly from `file://`; clicking
a node opens its Markdown file.

## Muse/TBH

Muse can use the repository directly through `.muse-plugin/plugin.json`, beside
the native Claude and Codex manifests:

```bash
tbh plugins validate . --json
tbh plugins install .
tbh plugins enable autoform
```

## Plugin surface

- `setup` creates or repairs the Markdown blueprint after confirming sources and scope.
- `orchestrate` works ready nodes with native subagents and Lean tools.
- `review` independently checks faithfulness, proof integrity, and Mathlib code quality.
- `autoform-lsp` provides Lean diagnostics and hover information.
- `autoform-repl` executes snippets through a persistent Lean process.

The four Lean tools require an absolute `project_dir`, so the plugin directory
is never mistaken for the user's Lean project. Stateless Mathlib or community
search stays outside the server surface and uses host-native tools when useful.

## Repository layout

```text
.claude-plugin/  Claude Code manifest
.codex-plugin/   Codex manifest
.muse-plugin/    Muse/TBH source manifest
skills/         three short expert nudges with on-demand review rubrics
autoform/       Markdown graph parser and validator
servers/        exactly two stateful servers: Lean LSP and REPL
visualization/  static HTML graph exporter
tests/          graph, packaging, and server contracts
```
