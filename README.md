# Autoform

Turn any AI coding assistant into a Lean 4 formalization agent — and plan
formalization efforts from textbooks.

Autoform gives your coding assistant the knowledge and tools to translate
mathematics from LaTeX into verified Lean 4 proofs using Mathlib (statement
extraction → proving → review), plus a **formalization planner** that builds a
tiered dependency graph from a textbook, maps it to Mathlib, and renders it as
an interactive leanblueprint (tier toggle, dependency-cone highlighting).

## Install

```text
/plugin marketplace add /home/niketp/rmt/autoform-bot
/plugin install autoform@autoform
```

(or point `/plugin marketplace add` at the git remote once pushed.)

## Prerequisites

- **Python ≥ 3.10**
- **[uv](https://docs.astral.sh/uv/)** — runs the formalization MCP servers
  (`uv run` resolves deps from `pyproject.toml` on first launch).
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- For the **planner's interactive blueprint view**: graphviz + a small Python
  toolchain — see [SETUP.md](SETUP.md).

## Skills

**Setup & workspace**

| Skill | What it does |
|-------|--------------|
| `/install-lean` | Install Lean 4, elan, lake (auto-runs via hook) |
| `/setup-project` | Create a new Lean 4 + Mathlib project |
| `/setup-autoform` | Check uv, Python deps, Lean 4, and Zulip — install what's missing |
| `/workspace` | Scan project structure, sorry/axiom counts, declarations |
| `/zulip` | Search Lean Zulip for community discussions |

**Planning**

| Skill | What it does |
|-------|--------------|
| `/plan` | Build a tiered dependency graph from a textbook, mapped to Mathlib |
| `/plan-view` | Build & open the interactive blueprint (tier toggle, cone highlighting) |

> The formalization skills (conventions, proving, review, extraction) ship as
> reference implementations under [`examples/`](examples/) and are being wired in
> incrementally — see [CONTRIBUTING.md](CONTRIBUTING.md).

## MCP Servers

| Server | Status | What it does |
|--------|--------|-------------|
| `lean-informal-planner-mathlib` | ✅ | Mathlib source search for the planner |
| `autoform-zulip` | ✅ | Search Lean Zulip for community discussions |
| `autoform-repl` | ⬜ stub | Lean 4 REPL — run code, verify proofs |
| `autoform-lsp` | ⬜ stub | Lean 4 LSP — diagnostics, type info |
| `autoform-aristotle` | ⬜ stub | Aristotle (Harmonic) — delegate to an autonomous prover |

## License

[MIT](LICENSE)

## Citation

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
