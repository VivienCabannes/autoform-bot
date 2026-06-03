# Autoform Plugin

## What it does

Autoform turns any AI coding assistant into a Lean 4 formalization agent. It provides Mathlib conventions, proof strategies, structured review checklists, and statement extraction — distilled from 94k PR reviews and 165k Zulip messages.

## Layout

```
autoform-bot/
├── README.md                          # Product pitch
├── LICENSE                            # MIT
├── package.json                       # npm package metadata
├── AGENTS.md                          # Multi-agent autodiscovery
├── GEMINI.md                          # Gemini CLI autodiscovery
├── gemini-extension.json              # Gemini CLI extension manifest
│
├── .claude-plugin/                    # Claude Code plugin manifest
│   ├── plugin.json                    #   Plugin metadata
│   └── marketplace.json               #   Marketplace listing
│
├── skills/                            # All skills (single source of truth)
│   ├── autoform/                      #   Core Mathlib & Lean 4 conventions
│   │   ├── SKILL.md
│   │   └── README.md
│   ├── autoform-prove/                #   Proof strategies & workflow
│   │   ├── SKILL.md
│   │   └── README.md
│   ├── autoform-review/               #   Formalization review checklist
│   │   ├── SKILL.md
│   │   └── README.md
│   ├── autoform-quality/              #   Code quality / style review
│   │   ├── SKILL.md
│   │   └── README.md
│   └── autoform-extract/              #   Statement extraction from LaTeX
│       ├── SKILL.md
│       └── README.md
│
├── agents/                            # Subagent definitions
│   ├── autoform-worker.md             #   Formalization agent (opus)
│   ├── autoform-reviewer.md           #   Code reviewer (opus)
│   └── autoform-reader.md             #   File reader (haiku)
│
└── commands/                          # Slash command stubs (Codex/Gemini)
    ├── autoform.toml
    ├── autoform-prove.toml
    ├── autoform-review.toml
    ├── autoform-quality.toml
    └── autoform-extract.toml
```

## Single source of truth

Edit skills in `skills/<name>/SKILL.md`. Everything else references these.

## Adding a new skill

1. Create `skills/<new-skill>/SKILL.md` with YAML frontmatter (`name`, `description`)
2. Create `skills/<new-skill>/README.md` for humans
3. Add a `commands/<new-skill>.toml` for Codex/Gemini
4. Add `@skills/<new-skill>/SKILL.md` to `AGENTS.md` and `GEMINI.md`
