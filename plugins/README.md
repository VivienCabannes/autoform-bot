# autoform — Lean 4 formalization plugin for Claude Code

A Claude Code plugin that repackages the **autoform-bot** pipeline as skills + slash commands.
Commands drive autoform-bot's multi-agent Python engine by default; `--backend native` runs a
lighter in-session loop with no autoform-bot checkout. The plugin bundles three auto-discovered
skills (Lean/Mathlib conventions, formalization workflow discipline, evaluation rubrics).

## Install

From a Claude Code session — the marketplace manifest lives at the repo root
(`.claude-plugin/marketplace.json`), so adding the repo just works:

```
/plugin marketplace add VivienCabannes/autoform-bot
/plugin install autoform@autoform-suite
```

(Local checkout instead of GitHub: `/plugin marketplace add /path/to/autoform-bot`.)

## Commands

| Command | Engine | Scope |
|---|---|---|
| `/autoform:extract` | python (default) | book → `targets.yaml` |
| `/autoform:orchestrate` | python (default) | whole-book run — the full engine |
| `/autoform:formalize` | in-session (native) | one target — write → review-gate → merge |
| `/autoform:eval` | python (default) | grade declarations (rubric jury + axiom check) |
| `/autoform:review` | in-session (native) | review a diff (read-only) |

## Skills (auto-discovered on `.lean` edits)

- **lean-conventions** — idiomatic Mathlib conventions + topic reference guides.
- **formalization-workflow** — axiom policy, `sorry` handling, builds, commit discipline.
- **eval-rubrics** — the jury rubrics (faithfulness / proof_integrity / code_quality …) and a
  complementary 7-dimension rater.

## How it was derived

- The skills lift autoform-bot's `autoform/bot/skills/{lean,mathlib,workflow}` and
  `autoform/eval/rubrics/*.json` into SKILL form.
- `agents/*` are translations of `autoform/{bot,eval,statement_extraction}/agents/*`
  (`prompt.md` + `config.yaml`): the config's `tools.servers` map to Claude Code tool allowlists,
  `model: Opus 4.6` → `model: opus`.

## Prerequisites

- **Default (python engine):** a working autoform-bot checkout with deps installed (`uv sync`), a
  `config.yaml` (see `autoform/bot/configs/`), and the inference API key(s) the run uses. Commands
  run `python -m autoform.{statement_extraction,bot.main,eval}` and surface the visualizer for
  progress.
- **`--backend native`:** no autoform-bot checkout — just a Lean/Mathlib project. Agents use the
  project's Lean LSP / REPL via MCP when available, else fall back to `lake` / `lake env lean`
  (mirroring lean4-skills' full / MCP-only / scripts-only profiles). Best for a single target or a
  quick scan.
