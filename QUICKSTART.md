# Quickstart

Autoform turns your AI coding assistant into a **Lean 4 formalization agent**.
It exposes three workflow commands backed by role agents, internal runbooks,
and persistent Lean LSP/REPL servers.

> For the full formalization workflow and command reference, see [docs/usage.md](docs/usage.md).
> For what's implemented vs. stubbed, see the status table in [README.md](README.md).

## Prerequisites

- **Claude Code** (shown here; Codex/Cursor/others in [README.md](README.md)).
- That's it — `make setup` installs `uv` + Python deps for you.
- **Lean 4** *(optional)* — Setup can install it when proof checking is needed.

## 1. Setup

From a local checkout:

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
make setup           # install Python deps (uv + MCP-server deps; assistant-agnostic)
make install-claude  # install the plugin into Claude Code (user scope)
```

After `make install-claude`, the plugin is live in Claude Code — no further install step.
Using Codex instead? Run `make install-codex`.

<details>
<summary>Manual / alternative install</summary>

Inside Claude Code:
```
/plugin marketplace add /path/to/autoform-bot
/plugin install autoform@autoform
```
Or load it for a single session without installing: `claude --plugin-dir /path/to/autoform-bot`.
Codex and npx installs (Cursor/Windsurf/Copilot/Cline) are in [README.md](README.md).
</details>

## 2. Point it at your Lean project

Launch the host from the Lean project. `LEAN_PROJECT_DIR` is also useful to the
workflow scripts:

```bash
cd /path/to/your/lean-project
LEAN_PROJECT_DIR="$PWD" claude --plugin-dir /path/to/autoform-bot
```

The MCP processes themselves start from the plugin directory. Their LSP/REPL
tools require the project's absolute path as `project_dir`, so they never
mistake the plugin checkout for the Lean project.

No Lean project yet? Setup creates one from the LeanProject template, or you
can try Autoform against the bundled sample at `examples/demo-project/`.

## 3. Use it — workflow skills

In Claude Code, use the slash commands below. In Codex, open the `$` skill
picker and choose the corresponding `$autoform:setup`,
`$autoform:orchestrate`, or `$autoform:set-backend` skill. This is the complete
user-visible surface:

| Command | What it does |
|---------|--------------|
| `/autoform:setup` | Install or repair prerequisites, create or inspect a project, plan sources, build the blueprint, and open the dashboard |
| `/autoform:orchestrate` | Prove, review, score, search prior art, handle escalations, and advance the plan |
| `/autoform:set-backend` | Choose and explain the prover backend, authentication, billing, and data path |

A good first move is `/autoform:setup`. Natural-language requests such as
“inspect this workspace,” “show the graph,” or “install Lean” stay inside Setup
instead of adding commands. Requests such as “review this node,” “prove this
theorem,” or “search for Lean prior art” stay inside Orchestrate.

For a repository that already has a roadmap, one Codex prompt can cover both
skills:

```text
Use AutoformBot to finish this repository's formalization roadmap. Treat
ROADMAP.md and its linked target packets as the confirmed source and scope.
Set up or resume the plan, then keep orchestrating with the Codex backend until
the confirmed scope is complete or a concrete blocker genuinely needs me.
```

Autoform records the confirmed roadmap ids, exact Lean targets, proof and review
fingerprints, and durable queue state before it can report completion.

## Developing the plugin

If you're **hacking on Autoform itself** (not just using it), there's a `Makefile`:

```bash
make demo      # run the workspace scanner on the sample project (no deps)
make test      # smoke tests — every MCP server constructs
make lint      # ruff over the Python implementation
make help      # all targets
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a server, skill, or agent.
