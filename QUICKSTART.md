# Quickstart

Autoform turns your AI coding assistant into a **Lean 4 formalization agent**.
It exposes three workflow commands backed by role agents, internal runbooks,
and MCP servers for Lean and Mathlib.

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

Set `LEAN_PROJECT_DIR` so the skills and servers know which project to work on, then launch:

```bash
LEAN_PROJECT_DIR=/path/to/your/lean-project claude --plugin-dir /path/to/autoform-bot
```

No Lean project yet? Setup creates one from the LeanProject template, or you
can try Autoform against the bundled sample at `examples/demo-project/`.

## 3. Use it — slash commands

Type these in the assistant. This is the complete user-visible surface:

| Command | What it does |
|---------|--------------|
| `/autoform:setup` | Install or repair prerequisites, create or inspect a project, plan sources, build the blueprint, and open the dashboard |
| `/autoform:orchestrate` | Prove, review, score, search prior art, handle escalations, and advance the plan |
| `/autoform:set-backend` | Choose and explain the prover backend, authentication, billing, and data path |

A good first move is `/autoform:setup`. Natural-language requests such as
“inspect this workspace,” “show the graph,” or “install Lean” stay inside Setup
instead of adding commands. Requests such as “review this node,” “prove this
theorem,” or “search Zulip” stay inside Orchestrate.

## 4. Optional unlocks

**Zulip search** — create `~/.zuliprc` (API key from
<https://leanprover.zulipchat.com/#settings/account>):

```ini
[api]
email=YOUR_ZULIP_EMAIL
key=YOUR_API_KEY
site=https://leanprover.zulipchat.com
```

Then `chmod 600 ~/.zuliprc`. Setup verifies connectivity.

---

## Developing the plugin

If you're **hacking on Autoform itself** (not just using it), there's a `Makefile`:

```bash
make demo      # run the workspace scanner on the sample project (no deps)
make test      # smoke tests — every MCP server constructs
make lint      # ruff over the Python implementation
make help      # all targets
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a server, skill, or agent.
