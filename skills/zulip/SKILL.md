---
name: zulip
description: >
  Search the Lean/Mathlib Zulip for community discussions before formalizing.
  Find naming conventions, proof strategies, prior art, and API decisions.
  Trigger: /zulip, "search zulip", "check zulip", "zulip discussion".
---

# Zulip

Search the Lean/Mathlib community Zulip before proving or naming anything non-trivial.

## When to search

- **Before naming** a new definition or theorem — check if a convention exists.
- **Before proving** — someone may have discussed the best approach or identified pitfalls.
- **When stuck** — the "Is there code for X?" stream is specifically for this.
- **Before adding a new file** — check if someone already has a PR in progress.

## How to use

1. Run `zulip_status` to verify configuration.
2. Use `zulip_search` with mathematical terms (e.g., `"Hoeffding bound"`, `"concentration inequality"`).
3. If a relevant topic is found, use `zulip_messages` to read the full thread.
4. Use `zulip_streams` to discover available streams if unsure where to look.

## Key streams for Mathlib work

- **mathlib4** — main development discussions
- **Is there code for X?** — ask before building from scratch
- **new members** — beginner questions, often about API discovery
- **general** — cross-cutting topics
- **Autoformalization** — autoformalization projects and tools

## Citing Zulip in code

When a Zulip discussion informed a design choice, add a comment:

```lean
/-- Hoeffding's inequality. See Zulip discussion:
https://leanprover.zulipchat.com/#narrow/stream/mathlib4/topic/Hoeffding -/
theorem hoeffding_bound ...
```
