# autoform — spec-driven Lean 4 autoformalization for Claude Code

Point autoform at a `sorry`, an `axiom`, or an informal textbook/paper, and it runs a complete
formalization workflow — plan → **spec gate** → prove → kernel-verify → reviewer packet —
entirely in-session on your Claude subscription. The design center is the **human reviewer**:
experts verify *statements* (the trust surface); the kernel verifies proofs; nobody reads
thousands of lines of generated tactic code.

## Install

The marketplace manifest lives at the repo root (`.claude-plugin/marketplace.json`):

```
/plugin marketplace add VivienCabannes/autoform-bot
/plugin install autoform@autoform-suite
```

(Local checkout instead of GitHub: `/plugin marketplace add /path/to/autoform-bot`.)

## Two commands

| Command | What it does |
|---|---|
| `/autoform:run <target>` | Auto-detects the target. Lean (`file.lean:123`, a decl, `AX_RiemannRoch`) → spec-gated prove loop. Informal (`book.md`, `paper.tex`, a PDF) → plan (dependency waves, Mathlib mapping) → spec-first statements → faithfulness jury → proof waves. Flags: `--dry-run` (plan only), `--spec-only` (stop at the human-signable spec), `--review-cycles N`, `--scope`, `--aristotle` / `--engine python` (paid, opt-in). |
| `/autoform:review [ref \| --staged \| --pr N \| --decl NAME] [--book-dir DIR]` | Builds the **reviewer packet**: spec sheet (must-read statements first, verbatim source + plain-math meaning side-by-side), kernel evidence (`lake env lean`, `#print axioms` deltas vs ledger, statement deltas for discharges), APPROVED/REJECTED verdict, and a 5-minute reading guide. `--score` runs the full rubric jury; `--quick` the 7-dimension rater; `--verify/--reject ISSUE` drive issue-tracked review gates. |

Migrating from 0.1.0: `extract` and `orchestrate` → `/autoform:run <book>` (`--dry-run` ≈
extract); `formalize` → `/autoform:run <target>`; `eval` → `/autoform:review --score`.

## Cost model

Default = **Claude subscription only**: every model call is the session or a Task subagent.
Two explicitly-flagged paid paths: `--aristotle` (Harmonic's prover, `ARISTOTLE_API_KEY`,
separate vendor) and `--engine python` (the legacy autoform-bot engine, raw API keys,
pay-per-token). Neither is ever chosen silently. If you keep `ANTHROPIC_API_KEY` exported in
your shell, the plugin's rules require scrubbing it from `claude -p` subprocesses so they bill
the subscription.

## Agents (dispatched by the commands; not user-facing)

`planner` (source → dependency-ordered plan with Mathlib status), `worker` (writes the Lean,
no-cheating rules), `code-reviewer` (faithfulness/honesty gate), `quality-inspector` (Mathlib
idiom), `matcher` (informal statement → Lean decl), `judge` (one rubric, one decl, strict JSON).

## Skills (auto-discovered on `.lean` edits)

- **lean-conventions** — idiomatic Mathlib conventions + topic reference guides (distilled from
  ~94k PR review comments / ~165k Zulip messages).
- **formalization-workflow** — axiom policy, `sorry` handling, builds, commit discipline, the
  reviewer-packet template, and the axiom-discharge protocol for challenge repos.
- **eval-rubrics** — jury rubrics (faithfulness / proof_integrity / code_quality …) plus the
  7-dimension rater.

## Prerequisites

A Lean 4 / Mathlib project (`lake` on PATH). Optional: Lean LSP / REPL MCP tools (used when
present), `gh` for PR/issue flows, the `marathon` CLI + `ARISTOTLE_API_KEY` for `--aristotle`,
an autoform-bot checkout + API keys for `--engine python`.

## Validation

The plugin is markdown + JSON, so validation is static + behavioural rather than unit tests:

- **`autoform/scripts/lint_plugin.py`** (stdlib only, run in CI by
  `.github/workflows/plugin-lint.yml`) — checks JSON validity, command/agent/skill frontmatter,
  that every `*.md` a skill cites exists, and that no reference survives to a command/agent
  removed in 0.2.0. Run locally: `python plugins/autoform/scripts/lint_plugin.py`.
- **`autoform/SMOKE_TEST.md`** — the repeatable end-to-end eval procedure (prove mode, review
  packet, formalize mode), with the 2026-06 jacobian-challenge reference run.
