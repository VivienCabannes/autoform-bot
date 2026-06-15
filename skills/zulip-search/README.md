# Zulip Search Skill

Search the Lean/Mathlib community Zulip for relevant discussions before formalizing.

## What it does

Teaches the agent to search Zulip for naming conventions, proof strategies,
prior art, and API decisions before writing Lean code. Lightweight trigger —
tells the agent when and how to search, with key streams to check.

## Usage

```
/zulip-search
```

Or naturally: "search Zulip for Hoeffding", "check if there's a Zulip discussion about this".

## Prerequisites

A `.zuliprc` file must be accessible. The plugin searches these locations in order:

1. `$ZULIPRC` env var
2. `$LEAN_PROJECT_DIR/.zuliprc`
3. `~/.zuliprc`
4. `~/.config/zulip/.zuliprc`
5. `~/.config/zuliprc`

Create one at https://leanprover.zulipchat.com/#settings/account — look for "API key".
