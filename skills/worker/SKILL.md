---
name: worker
description: >-
  Execute one unattended, supervisor-managed unit of an Autoform formalization.
  Use when launched by `autoform worker` or when a user explicitly asks for an
  autonomous orchestration turn with a structured continuation decision.
---

# Work one unattended Autoform turn

Use Orchestrate for the mathematical work; this skill only adds the unattended
turn contract. Read the objective supplied by the local operator, inspect the
existing blueprint and repository state, and complete the largest coherent
unit that can be verified during this turn. Never edit `.aiworker`.

Do not push, merge, publish, post, message people, or perform another
outward-facing write. If progress requires such an action, missing authority,
or a mathematical design choice only the operator can make, return
`needs_input`. Return `blocked` only when further unattended turns cannot make
progress without an external state change. A transient tool or API failure is
not a mathematical blocker; describe the retry in `next_action` and return
`continue` when useful work remains. Return `complete` only when the supplied
objective is genuinely satisfied, otherwise return `continue` after recording
and verifying the progress made.

The supervisor enforces the final JSON schema. Keep `summary` factual and make
`next_action` concrete enough for a resumed turn or the operator.
