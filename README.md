# Autoform

Turn any AI coding assistant into a Lean 4 formalization agent.

Autoform gives your coding assistant the knowledge and tools to translate mathematics from LaTeX into verified Lean 4 proofs using Mathlib — from statement extraction through proof completion and review.

## Status: Template

This repo ships as a **working minimal plugin**. The wiring (manifests, hooks, commands, discovery files) is complete. The domain content is stubbed:

| Component | Status | What's there | What PRs add |
|-----------|--------|-------------|--------------|
| **Workspace server** | ✅ Full | Project scan, sorry/axiom counts, declarations | — |
| **Other servers** (repl, mathlib, lsp, trace, aristotle) | ⬜ Stub | Servers start, tools return "not implemented" | Real implementations |
| **Skills** (6) | ⬜ Stub | Section headings + 2-3 rules each | Full tactic tables, checklists, pitfall lists |
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
/plugin marketplace add https://github.com/vivc/autoform-bot.git
/plugin install autoform@autoform
```

Or from a local checkout:

```
/plugin marketplace add /path/to/autoform-bot
/plugin install autoform@autoform
```

### Codex CLI

The plugin includes a `.codex-plugin/plugin.json` manifest and `commands/*.toml` slash commands.

### Other agents (via npx skills)

| Agent | Install |
|-------|---------|
| Cursor | `npx skills add vivc/autoform-bot -a cursor` |
| Windsurf | `npx skills add vivc/autoform-bot -a windsurf` |
| Copilot | `npx skills add vivc/autoform-bot -a github-copilot` |
| Cline | `npx skills add vivc/autoform-bot -a cline` |

## Skills

| Skill | Slash command | What it does |
|-------|--------------|--------------|
| Mathlib conventions | `/autoform` | Lean 4 + Mathlib style, tactics, naming, pitfalls |
| Proof strategies | `/autoform-prove` | Incremental proving workflow: search → prototype → prove → commit |
| Code review | `/autoform-review` | Review for correctness, faithfulness, and cheating patterns |
| Quality check | `/autoform-quality` | Mathlib style lint — naming, tactics, code structure |
| Statement extraction | `/autoform-extract` | Extract formalizable statements from LaTeX/Markdown |
| Crew orchestration | `/autoform-crew` | Parallel formalization with subagent teams |

## MCP Servers

| Server | Status | What it does |
|--------|--------|-------------|
| `autoform-workspace` | ✅ | Scan project structure, sorry/axiom counts, targets |
| `autoform-repl` | ⬜ | Lean 4 REPL — run code, verify proofs |
| `autoform-mathlib` | ⬜ | Mathlib source search — grep, find by name, read files |
| `autoform-lsp` | ⬜ | Lean 4 LSP — file diagnostics, type info |
| `autoform-trace` | ⬜ | Execution tracing — record proof attempts, reviews |
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
