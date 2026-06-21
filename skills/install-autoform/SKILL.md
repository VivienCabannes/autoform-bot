---
name: install-autoform
description: >
  Set up the full autoform environment — uv, Python dependencies, Lean 4,
  and optional Zulip access. Checks each component and installs what's missing.
  Trigger: /install-autoform, "setup autoform", "install autoform", "setup environment".
---

# Set Up Autoform

Run the setup script:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/install-autoform/install-autoform.sh"
```

The script is idempotent — safe to re-run. It checks and sets up:

1. **uv** — Python package manager (required for all MCP servers)
2. **Python dependencies** — resolves all packages from `pyproject.toml` via `uv run`
3. **Lean 4** — checks for `lean` and `lake` on PATH (suggests `/install-lean` if missing)
4. **Zulip** (optional) — checks for `.zuliprc` credentials and tests connectivity

If any component is missing, the script prints clear instructions for fixing it.
Zulip is treated as optional — the script warns but does not fail if `.zuliprc` is absent.
