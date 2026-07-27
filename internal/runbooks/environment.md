# Environment setup runbook

Resolve an absolute plugin root from a valid `AUTOFORM_PLUGIN_ROOT`,
`PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
`Path(<this loaded SKILL.md>).resolve().parents[2]`.

Run the setup script:

```bash
bash "<AUTOFORM_PLUGIN_ROOT>/scripts/install_autoform.sh"
```

The script is idempotent — safe to re-run. It checks and sets up:

1. **uv** — Python package manager (required for all MCP servers)
2. **Python dependencies** — resolves all packages from `pyproject.toml` via `uv run`
3. **Lean 4** — checks for `lean` and `lake` on PATH (Setup can run the
   internal Lean installer if missing)
4. **Zulip** (optional) — checks for `.zuliprc` credentials and tests connectivity
5. **Lean Explore** (optional) — checks for `LEANEXPLORE_API_KEY` (a key from
   https://www.leanexplore.com), which enables semantic Mathlib search via the **lean-explore**
   integration; unset is fine — semantic search is simply skipped

If any component is missing, the script prints clear instructions for fixing it.
The optional components (Zulip, Lean Explore) warn but never fail the setup if absent.
