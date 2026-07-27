---
name: install-autoform
description: >
  Install or repair Autoform's own environment prerequisites: uv, Python
  dependencies, Lean 4, and optional Zulip access. Use for toolchain/plugin
  installation, not for creating or resuming a formalization project.
---

# Set Up Autoform

Resolve an absolute plugin root from a valid `AUTOFORM_PLUGIN_ROOT`,
`PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
`Path(<this loaded SKILL.md>).resolve().parents[2]`.

Run the setup script:

```bash
bash "<AUTOFORM_PLUGIN_ROOT>/skills/install-autoform/install-autoform.sh"
```

The script is idempotent — safe to re-run. It checks and sets up:

1. **uv** — Python package manager (required for all MCP servers)
2. **Python dependencies** — resolves all packages from `pyproject.toml` via `uv run`
3. **Lean 4** — checks for `lean` and `lake` on PATH (suggests `/install-lean` if missing)
4. **Zulip** (optional) — checks for `.zuliprc` credentials and tests connectivity
5. **Lean Explore** (optional) — checks for `LEANEXPLORE_API_KEY` (a key from
   https://www.leanexplore.com), which enables semantic Mathlib search via the **lean-explore**
   skill; unset is fine — the skill simply isn't used

If any component is missing, the script prints clear instructions for fixing it.
The optional components (Zulip, Lean Explore) warn but never fail the setup if absent.
