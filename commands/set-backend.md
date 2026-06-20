---
description: Set the prover backend used by /autoform:dispatch (max | aristotle | codex) — persisted, so you set it once. Backend is also the billing path (max = Max subscription, never the API). Shared with the dashboard's backend dropdown.
argument-hint: "[max | aristotle | codex]   # no arg → show current + options"
allowed-tools: Bash
---

# /autoform:set-backend — choose the prover backend

The unified prover MCP (`servers/prover`) treats the backend as a **swappable parameter**: the
orchestrator (your Claude Code session) stays the brain; only the backend that *proves a node*
changes. This command persists which backend `/autoform:dispatch` uses, so you pick it **once**
instead of passing `--backend` each time. The DAG review dashboard's **backend dropdown reads/writes
the same config**, so UI and CLI stay in sync. **Backend is also the billing path:**

- `max` → the **Max subscription** (the prover's `claude` adapter, `ANTHROPIC_API_KEY` scrubbed) — no API tokens.
- `aristotle` → Harmonic's Aristotle (`ARISTOTLE_API_KEY`).
- `codex` → its own auth *(planned — adapter not yet implemented; selecting it warns)*.

Use the helper `scripts/backend_config.py`, always via `env -u ANTHROPIC_API_KEY python3
scripts/backend_config.py`:

- **No argument** → `backend_config.py list` (the `*` marks the current backend; each line shows the
  `prove_node backend=…` it maps to + its billing). Echo it as-is.
- **`<backend>`** (`max` | `aristotle` | `codex`) → `backend_config.py set <backend>`, then **echo the
  resulting backend, the `prove_node` id it maps to, and its billing path** so the user sees exactly
  what `/autoform:dispatch` will run on. An unknown backend errors with the known list; `codex`
  persists but warns its adapter isn't built yet.

The setting lives at `~/.autoform/config.json` (override with `$AUTOFORM_CONFIG`).
`/autoform:dispatch` reads it on each run; an explicit `--backend` on dispatch still overrides it.
