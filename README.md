# AutoformBot

AutoformBot is a Claude Code and Codex plugin for turning mathematical sources
into a Lean 4 formalization and a readable companion site. It provides:

- repository setup for Lean, Mathlib, CI, and GitHub Pages;
- source-grounded Markdown roadmaps with explicit theorem dependencies;
- exhaustive source-unit coverage checks;
- shared Lean LSP and REPL tools;
- human and independent agent review workflows; and
- claim-backed orchestration for several workers in separate Git worktrees.

The plugin and Python commands use the name `autoform`. The canonical repository
is [`facebookresearch/autoform-bot`](https://github.com/facebookresearch/autoform-bot).

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Git
- Lean and Lake for proof checking
- Claude Code or Codex

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

Start a new agent session after installation so the skills and MCP servers are
loaded. A Muse manifest is bundled, but Muse installation is separate.

## Use the plugin

Invoke the skill that matches the current stage:

| Task | Claude Code | Codex |
| --- | --- | --- |
| Set up or repair a repository | `/autoform:setup` | `$setup` |
| Build or refine the roadmap | `/autoform:roadmap` | `$roadmap` |
| Inspect the rendered plan yourself | `/autoform:human-review` | `$human-review` |
| Run an independent audit | `/autoform:agent-review` | `$agent-review` |
| Work through ready nodes | `/autoform:orchestrate` | `$orchestrate` |

A typical request is:

> Use Autoform to set up this Lean repository, build a roadmap for every result
> in `sources/book.pdf`, have an independent agent audit it, then formalize the
> ready nodes and maintain the readable companion.

Autoform keeps authored state in an Obsidian-compatible blueprint vault. Each
roadmap article records its sources, statement dependencies, proof dependencies,
and verified Lean declarations. Status, graphs, and publication pages are
derived from those files.

## Create a project

For a new Lean repository, let the Setup skill select the bundled compatible
Lean and Mathlib release. The underlying commands are:

```bash
autoform project provenance --json
autoform project versions --json
autoform project new MyProject --package MyProject \
  --release <release-id> \
  --autoform-source https://github.com/facebookresearch/autoform-bot.git \
  --autoform-ref <full-commit-sha>
```

For an existing repository that needs several independent formalization
projects, create a workspace and register each blueprint:

```bash
autoform workspace init . --blueprint-root docs/blueprints
autoform blueprint new textbook --path Textbook --title "Textbook"
autoform workspace inspect .
autoform workspace check . --lean-root .
```

The root `.autoform.toml` is the only workspace registry. Registered blueprint
paths must not overlap. The original single-project layout remains available:

```bash
autoform init . \
  --autoform-source https://github.com/facebookresearch/autoform-bot.git \
  --autoform-ref <full-commit-sha>
```

## Validate and publish

Run these commands from the Lean project. Add `--project <id>` when selecting a
project from a multi-project workspace.

```bash
autoform doctor . --lean-root .
autoform check blueprint --lean-root .
autoform audit blueprint --lean-root .
lake build
autoform render blueprint --output site-src \
  --lean-root . --require-declarations
```

`check` validates the Markdown dependency graph. `audit` checks roadmap and
source coverage. `lake build` checks Lean. `render` produces MkDocs source for
the human-readable companion; the generated Pages workflow can publish it once
GitHub Pages is enabled.

For the complete blueprint format and command flags, see the
[CLI reference](autoform_cli/README.md).

## Collaborate safely

The Orchestrate skill assigns independent roadmap leaves to specialist agents.
Each writer uses its own Git worktree and a fail-closed Git-ref claim:

```bash
export AUTOFORM_WORKER_ID="worker-name"
autoform claim acquire <node-id>
autoform claim renew <node-id>
autoform claim release <node-id>
```

Use `autoform claim list` to inspect ownership. Workers also claim shared
resources such as `lake-build` before using a shared build cache. A result is
eligible for integration only after the fixed Lean and source-contract gates
pass and an independent reviewer accepts it.

The `autoform-worker` command runs one claim-backed scheduling round when a
direct worker process is useful:

```bash
autoform-worker --project . --claim-repo <git-repository> \
  --backend codex --model <exact-model-id>
```

Use `--workspace-project <id>` for a registered workspace project. Runs pin the
source coverage contract, roadmap generation, toolchain, model identities, and
candidate evidence so interrupted work can be inspected and resumed without
silently changing inputs.

## Development

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
make setup
make lint
make test
make check-example
```

Plugin maintainers can use `/autoform:develop-plugin` in Claude Code or
`$develop-plugin` in Codex. AutoformBot is released under the
[MIT License](LICENSE).
