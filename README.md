# AutoformBot

AutoformBot is a coding-agent plugin and Python CLI for Lean 4 formalization
projects. It builds source-grounded Markdown roadmaps, validates dependencies,
publishes progress views, and prepares human or agent review. The plugin and
CLI use the identifier `autoform`; the canonical repository is
[`facebookresearch/autoform-bot`](https://github.com/facebookresearch/autoform-bot).

The default `main` branch provides repository setup, roadmap planning,
publication, human and agent review, and shared Lean LSP/REPL tools. It does
**not** include autonomous orchestration.

Autonomous execution is an opt-in overlay on the
[`execution`](https://github.com/facebookresearch/autoform-bot/tree/execution)
branch. It adds orchestration, claim-backed workers, specialist agents, and
prover adapters on top of `main`. Use `main` unless you are explicitly
evaluating that execution stack.

## Prerequisites

- Python 3.10 or newer and [`uv`](https://docs.astral.sh/uv/)
- Git
- Lean and Lake for Lean tooling and verification
- Claude Code or Codex for the installation flows below

## Install

Claude Code:

```bash
claude plugin marketplace add facebookresearch/autoform-bot
claude plugin install autoform@autoform
```

Codex:

```bash
codex plugin marketplace add facebookresearch/autoform-bot --ref main
codex plugin add autoform@autoform
```

Start a new agent session so the skills and MCP servers reload. A native Muse
manifest is included, but Muse installation is not covered here.

## Quick start

For a repository that may contain more than one formalization effort, create a
workspace manifest and then register each blueprint explicitly:

```bash
autoform workspace init /path/to/lean-project --blueprint-root docs/blueprints
autoform blueprint new finite-flat --workspace /path/to/lean-project \
  --path FiniteFlat --title "Finite Flat Group Schemes"
autoform workspace inspect /path/to/lean-project
autoform workspace check /path/to/lean-project --lean-root /path/to/lean-project
```

The root `.autoform.toml` is the sole registry. Autoform does not scan sibling
directories or require a marker inside each vault. Location names and paths are
repository-defined; `docs/blueprints` above is only an example. Registered
vaults must have non-overlapping portable paths, and commands never infer a
project from an unrelated directory.

For a dedicated repository that intentionally uses the original single-vault
layout, first verify the loaded plugin's source and exact commit, then use the
legacy scaffold command:

```bash
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform project provenance --json
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform init /path/to/lean-project \
  --autoform-source <credential-free-https-git-url> \
  --autoform-ref <full-commit-sha>
```

This creates `blueprint/`, `mkdocs.yml`, and `requirements-docs.txt`. GitHub
workflows are created only when both values are present. A plain wheel cannot
infer them. The Setup skill guides repository inspection, Lean/Mathlib shell
preparation, workspace setup, and this backwards-compatible `autoform init`
flow.

Next use the host skills from the Lean project:

| Goal | Claude Code | Codex |
| --- | --- | --- |
| Build a source-grounded roadmap | `/autoform:roadmap` | `$roadmap` |
| Prepare a person-led review | `/autoform:human-review` | `$human-review` |
| Run an independent agent review | `/autoform:agent-review` | `$agent-review` |

For example: “Build a roadmap for Sections 2–4 of `paper.pdf`; confirm the scope
and completion criteria before writing articles.” Keep the source in the
repository or provide an accessible path. Human and agent review are
alternatives; review the roadmap before treating it as an execution plan.

## Blueprint model

A workspace can hold several independent vaults:

```text
.autoform.toml
docs/blueprints/
├── FiniteFlat/
│   ├── README.md
│   ├── coverage/README.md
│   ├── roadmap/README.md
│   └── sources/README.md
└── AnotherProject/
    └── ...
```

Each registered project has the same internal vault model. In a legacy
single-vault repository that model lives directly at:

```text
blueprint/
├── README.md
├── coverage/README.md
├── roadmap/
│   ├── README.md
│   └── convexity/
│       ├── README.md
│       ├── convex.md
│       └── separating-hyperplane.md
└── sources/paper.md
```

Every Markdown file below the selected vault's `roadmap/` is an article. A nested
`README.md` represents its directory and contains the articles below it.
Optional `declaration: theorem`, `declaration: def`, and similar frontmatter
marks a formalizable article. Inline relative links under `## Depends on` and
`## Proof depends on` define dependency edges; reference-style links do not.

Markdown is the source of truth; Mermaid graphs and MkDocs pages are derived
views. See the [blueprint format and CLI reference](autoform_cli/README.md) for
complete frontmatter, hierarchy, status, and validation rules.

## CLI and publication

| Command | Purpose |
| --- | --- |
| `autoform workspace init` | Create a root manifest and blueprint collection. |
| `autoform workspace inspect` | Inspect registered locations and projects without scanning siblings. |
| `autoform workspace check` | Validate every registered blueprint. |
| `autoform blueprint new` | Create and centrally register one blueprint vault. |
| `autoform blueprint register` | Register an existing vault without modifying it. |
| `autoform blueprint list` | List centrally registered blueprints. |
| `autoform init` | Scaffold the legacy single-vault layout and publication files. |
| `autoform check` | Validate Markdown structure and dependencies. |
| `autoform audit` | Audit completeness and checked facts. |
| `autoform doctor` | Diagnose the local blueprint contract. |
| `autoform project new` | Atomically create a complete Lean and Autoform project. |
| `autoform project provenance` | Verify the loaded plugin's immutable source and commit. |
| `autoform claim` | Coordinate temporary ownership through Git refs. |
| `autoform render` | Generate publishable MkDocs source. |
| `autoform-visualize` | Generate the Mermaid dependency graph. |

Inside an Autoform checkout, use `uv run`:

```bash
uv run autoform check /path/to/project/blueprint --lean-root /path/to/project
uv run autoform-visualize /path/to/project/blueprint
uv run autoform render /path/to/project/blueprint \
  --output /path/to/project/site-src \
  --lean-root /path/to/project --require-declarations
```

From a consumer project, resolve the installed plugin root and prefix commands
with `uv run --project "<AUTOFORM_PLUGIN_ROOT>"`, or separately install the
Python package so its console scripts are on `PATH`.

`check --lean-root` lexically resolves names in local Lean files; it does not
compile them or prove that they belong to a Lake target. Use `lake build` and
the verification workflow for compilation and audit. That gate binds local
claims to the root package's artifacts. It accepts Mathlib claims only from a
clean checkout at the manifest-pinned commit of the canonical upstream
`https://github.com/leanprover-community/mathlib4.git`, then checks the module's
Lake package trace.

`render` writes MkDocs source, not a deployed site. The generated Pages workflow
deploys from `main` only after GitHub Pages is enabled in repository settings.

## Documentation

- [Cabannes thesis example](skills/setup/assets/cabannes-thesis-project/README.md)
- [Roadmap example](skills/roadmap/references/cabannes-thesis-roadmap.md)
- [Lean server architecture and operations](servers/README.md)

## Development

Development also requires Make:

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
make setup
make lint
make test
make check-example
```

Claude Code uses `/autoform:develop-plugin`; Codex uses `$develop-plugin`.
`make check-example` validates, renders, and builds the example documentation.
Run `lake build` in the Cabannes fixture when changing its Lean sources or
declarations.

AutoformBot is released under the [MIT License](LICENSE).
