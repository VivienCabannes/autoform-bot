# Autoform

Turn any AI coding assistant into a Lean 4 formalization agent.

Autoform gives your coding assistant the knowledge and tools to translate mathematics from LaTeX into verified Lean 4 proofs using Mathlib — from statement extraction through proof completion and review.

## Status: Template

This repo ships as a **working minimal plugin**. The wiring (manifests, hooks, discovery files) is complete. Domain-specific skills (conventions, proof strategies, review checklists) are left for future PRs:

| Component | Status | What's there | What PRs add |
|-----------|--------|-------------|--------------|
| **Setup skills** (install-lean, setup-project) | ✅ Full | Scripts + hook-driven auto-execution | — |
| **Workspace** (skill + script) | ✅ Full | Project scan, sorry/axiom counts, declarations | — |
| **Zulip** (server + skill) | ✅ Full | Search Lean Zulip for community discussions | — |
| **Servers** (repl, lsp, aristotle) | ⬜ Stub | Servers start, tools return "not implemented" | Real implementations |
| **Formalization skills** | ⬜ Not yet | — | Conventions, proof strategies, review, extraction, orchestration |
| **Agents** (3) | ⬜ Stub | Correct frontmatter, one-paragraph prompts | Rich system prompts |

Full reference implementations for every stub live in [`examples/`](examples/). See [CONTRIBUTING.md](CONTRIBUTING.md) for how to pick up a stub and fill it in.

## Before / After

**Before** — you paste a theorem and your assistant writes broken Lean:
```
You: "Formalize Theorem 2.3 from this chapter"
Assistant: *writes Lean with wrong types, missing imports, sorry everywhere*
```

**After** — your assistant knows Mathlib conventions, searches before proving, and self-reviews:
```
You: "Formalize Theorem 2.3 from this chapter"
Assistant: *searches Mathlib for existing lemmas, uses correct typeclasses,
           writes idiomatic proofs, flags what the book leaves unproved*
```

## Install

### Claude Code

Inside Claude Code, add the marketplace and install:

```
/plugin marketplace add https://github.com/facebookresearch/autoform-bot.git
/plugin install autoform@autoform
```

Or from a local checkout:

```
/plugin marketplace add /path/to/autoform-bot
/plugin install autoform@autoform
```

### Codex CLI

Codex installs plugins from a marketplace root. For a local checkout, create a
small local marketplace that points back to this repo:

```sh
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot

MARKETPLACE_ROOT="${CODEX_AUTOFORM_MARKETPLACE:-$HOME/.autoform-codex-marketplace}"
mkdir -p "$MARKETPLACE_ROOT/plugins" "$MARKETPLACE_ROOT/.agents/plugins"

if [ -L "$MARKETPLACE_ROOT/plugins/autoform" ]; then
  rm "$MARKETPLACE_ROOT/plugins/autoform"
fi
ln -s "$PWD" "$MARKETPLACE_ROOT/plugins/autoform"

cat > "$MARKETPLACE_ROOT/.agents/plugins/marketplace.json" <<'JSON'
{
  "name": "autoform-local",
  "interface": {
    "displayName": "AutoForm Local"
  },
  "plugins": [
    {
      "name": "autoform",
      "source": {
        "source": "local",
        "path": "./plugins/autoform"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Coding"
    }
  ]
}
JSON

codex plugin marketplace add "$MARKETPLACE_ROOT"
codex plugin add autoform@autoform-local
```

Verify the install:

```sh
codex plugin list | grep autoform
```

The Codex plugin name is `autoform`. Start a new Codex thread after installing
or reinstalling so the new skills and MCP servers are loaded.

### Other agents (via npx skills)

| Agent | Install |
|-------|---------|
| Cursor | `npx skills add facebookresearch/autoform-bot -a cursor` |
| Windsurf | `npx skills add facebookresearch/autoform-bot -a windsurf` |
| Copilot | `npx skills add facebookresearch/autoform-bot -a github-copilot` |
| Cline | `npx skills add facebookresearch/autoform-bot -a cline` |

## Skills

| Skill | Slash command | What it does |
|-------|--------------|--------------|
| Install Lean | `/install-lean` | Install Lean 4, elan, lake — auto-runs via hook |
| Setup project | `/setup-project` | Create a new Lean 4 + Mathlib project from the LeanProject template |
| Workspace | `/workspace` | Scan project structure, sorry/axiom counts, declarations |
| Zulip | `/zulip` | Search Lean Zulip for community discussions |

## MCP Servers

| Server | Status | What it does |
|--------|--------|-------------|
| `autoform-zulip` | ✅ | Search Lean Zulip for community discussions |
| `autoform-repl` | ⬜ | Lean 4 REPL — run code, verify proofs |
| `autoform-lsp` | ⬜ | Lean 4 LSP — file diagnostics, type info |
| `autoform-aristotle` | ⬜ | Aristotle (Harmonic) — delegate to autonomous prover |

## Agents

| Agent | Model | Role |
|-------|-------|------|
| `autoform-worker` | opus | Formalization — reads source, searches Mathlib, writes proofs |
| `autoform-reviewer` | opus | Reviews for correctness, faithfulness, and cheating patterns |
| `autoform-reader` | haiku | Lightweight file reader for large files |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to pick up a stub, fill it in, and submit a PR. Each server, skill, and agent is independent — you can contribute one without touching the others.

## License

[MIT](LICENSE)

## Citation

If you find this work useful, please cite our paper:

```bibtex
@misc{rammal2026formalizingmathematicsscale,
      title={Formalizing Mathematics at Scale},
      author={Ahmad Rammal and Niket Patel and Fabian Gloeckle and Amaury Hayat and Julia Kempe and Remi Munos and Charles Arnal and Vivien Cabannes},
      year={2026},
      eprint={2605.29955},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2605.29955},
}
```
