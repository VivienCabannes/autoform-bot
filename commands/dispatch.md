---
description: Alias of /autoform:orchestrate (renamed). Launches the deterministic dispatch engine — parallel review jury + prover workers — over the dashboard's task queue, and (by default) autonomously drives the work.
argument-hint: "[<review-project-dir>] [--manual] [--max-tasks N] [--backend max|aristotle|codex] [--once]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, Skill
---

# /autoform:dispatch → renamed to /autoform:orchestrate

This command is now **`/autoform:orchestrate`**. Read `${CLAUDE_PLUGIN_ROOT}/commands/orchestrate.md` and follow it **exactly**, passing `$ARGUMENTS` through unchanged. Same behavior — the new name reflects that it both runs the dispatch engine and can autonomously drive the formalization.
