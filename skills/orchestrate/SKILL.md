---
name: orchestrate
description: Run or resume an Autoform plan through planning, proving, review, escalation, prior-art search, and deterministic completion.
---

Start or reuse `scripts/dispatch_runner.py` for engine-owned reviewer and worker tasks, handle `scripts/dispatch_queue.py <project> mine` with native subagents, and search Mathlib or Lean community prior art with the host's available tools when useful. Keep API egress explicit and report completion only when the queue is empty and `scripts/check_completion.py` passes.
