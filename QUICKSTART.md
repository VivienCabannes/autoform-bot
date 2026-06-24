# Quickstart

Autoform turns your AI coding assistant into a **Lean 4 formalization agent** — it adds
skills, slash commands, and MCP servers for working with Lean and Mathlib. This guide gets
the plugin running **inside your assistant** in a few minutes.

> For the full formalization workflow and command reference, see [docs/usage.md](docs/usage.md).
> For what's implemented vs. stubbed, see the status table in [README.md](README.md).

## Prerequisites

- **Claude Code** (shown here; Codex/Cursor/others in [README.md](README.md)).
- That's it — `make setup` installs `uv` + Python deps for you.
- **Lean 4** *(optional)* — only for the proof-checking servers. Install later with `/install-lean`
  (or `elan default stable`).

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

No Lean project yet? Create one from inside the assistant with `/setup-project MyBook`, or try it
against the bundled sample at `examples/demo-project/`.

## 3. Use it — slash commands

Type these in the assistant. **Working today:**

| Command | What it does |
|---------|--------------|
| `/workspace` | Triage the project — file/declaration counts, `sorry`/`axiom` tallies, the targets DAG |
| `/zulip` | Search the Lean community Zulip for naming, proofs, prior art *(needs `~/.zuliprc`, below)* |
| `/install-lean` | Install Lean 4, elan, lake |
| `/setup-project MyBook` | Scaffold a new Lean 4 + Mathlib project |
| `/setup-autoform` | Check/install the full environment (uv, deps, Lean, Zulip) |

A good first move: `/workspace` — it scans `$LEAN_PROJECT_DIR` and hands the structure to the
agent, so you can ask things like *"which targets have no dependencies yet?"*

> **Not yet active on `main`:** `/autoform`, `/autoform-prove`, `/autoform-review`, and the
> `repl`/`lsp`/`aristotle` MCP tools return "not implemented" — these are the formalization
> components still being filled in. The end-to-end vision (extract → formalize → prove → review)
> is described in [docs/usage.md](docs/usage.md).

## 4. Optional unlocks

**Zulip search** — create `~/.zuliprc` (API key from
<https://leanprover.zulipchat.com/#settings/account>):

```ini
[api]
email=YOUR_ZULIP_EMAIL
key=YOUR_API_KEY
site=https://leanprover.zulipchat.com
```

Then `chmod 600 ~/.zuliprc`. Inside the assistant, `/setup-autoform` verifies connectivity.

---

## Developing the plugin

If you're **hacking on Autoform itself** (not just using it), there's a `Makefile`:

```bash
make demo      # run the workspace scanner on the sample project (no deps)
make test      # smoke tests — every MCP server constructs
make lint      # ruff over servers/ and skills/
make help      # all targets
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a server, skill, or agent.
