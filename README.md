# Autoform

Turn any AI coding assistant into a Lean 4 formalization agent.

Autoform gives your coding assistant the knowledge and tools to translate mathematics from LaTeX into verified Lean 4 proofs using Mathlib — from statement extraction through proof completion and review.

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

```bash
claude plugin add vivc/autoform-bot
```

### Other agents (via npx skills)

| Agent | Install |
|-------|---------|
| Cursor | `npx skills add vivc/autoform-bot -a cursor` |
| Windsurf | `npx skills add vivc/autoform-bot -a windsurf` |
| Copilot | `npx skills add vivc/autoform-bot -a github-copilot` |
| Cline | `npx skills add vivc/autoform-bot -a cline` |

## What You Get

| Skill | Slash command | What it does |
|-------|--------------|--------------|
| Mathlib conventions | `/autoform` | Lean 4 + Mathlib style, tactics, naming, pitfalls — distilled from 94k PR reviews |
| Proof strategies | `/autoform-prove` | Incremental proving workflow: search → prototype → prove → commit |
| Code review | `/autoform-review` | Review Lean formalization for correctness, faithfulness, and cheating patterns |
| Quality check | `/autoform-quality` | Mathlib style lint — naming, tactics, code structure |
| Statement extraction | `/autoform-extract` | Extract formalizable statements from LaTeX/Markdown source material |
| Crew orchestration | `/autoform-crew` | Parallel formalization with subagent teams — wave-based chapter proving |

## Skills in detail

<details>
<summary><strong>autoform</strong> — Mathlib & Lean 4 conventions</summary>

Core knowledge base: proof style, naming, types, tactics, simp conventions, API design, code style, and common pitfalls. Distilled from ~94k GitHub PR review comments and ~165k Zulip messages from the Mathlib community.

Activate with `/autoform` or by asking about Lean 4 conventions.

</details>

<details>
<summary><strong>autoform-prove</strong> — Proof strategies & workflow</summary>

How to approach Lean proofs: search Mathlib first, prototype in REPL, work incrementally, handle sorry/axiom correctly, detect false statements. Includes the `unproved` macro policy for statements the source material leaves unproven.

Activate with `/autoform-prove`.

</details>

<details>
<summary><strong>autoform-review</strong> — Formalization review</summary>

Structured review checklist: compilation, faithfulness to source, mathematical correctness, conventions, and anti-cheating detection (trivial substitution, smuggled assumptions, weakened content, modeling avoidance, hidden sorry/axiom).

Activate with `/autoform-review`.

</details>

<details>
<summary><strong>autoform-quality</strong> — Code quality inspection</summary>

Pure style review — naming, tactic usage, proof structure, Mathlib conventions. Does not evaluate mathematical correctness (that's autoform-review's job).

Activate with `/autoform-quality`.

</details>

<details>
<summary><strong>autoform-extract</strong> — Statement extraction</summary>

Extract definitions, theorems, lemmas, and corollaries from LaTeX or Markdown source material into structured YAML targets for formalization.

Activate with `/autoform-extract`.

</details>

<details>
<summary><strong>autoform-crew</strong> — Parallel formalization</summary>

Orchestration guide for subagent teams. Fan out workers across independent targets, batch reviews, wave-based chapter formalization. Tells the main thread when to delegate vs. do it inline.

Activate with `/autoform-crew`.

</details>

## Agents

Autoform includes specialized subagents for multi-agent workflows:

| Agent | Model | Role |
|-------|-------|------|
| `autoform-worker` | opus | Lean 4 formalization — reads source, searches Mathlib, writes proofs |
| `autoform-reviewer` | opus | Reviews changes for correctness, faithfulness, and cheating patterns |
| `autoform-reader` | haiku | Lightweight file reader for large files (small context, fast) |

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
