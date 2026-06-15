---
name: setup-zulip
description: >
  Set up Zulip access for searching the Lean/Mathlib community discussions.
  Checks prerequisites (uv), verifies .zuliprc credentials, and tests connectivity.
  Trigger: /setup-zulip, "setup zulip", "configure zulip", "zulip not configured".
---

# Set up Zulip

Run the setup script:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/setup-zulip/setup-zulip.sh"
```

The script is idempotent — safe to re-run. It:

1. Checks that `uv` is installed (required to run the MCP server)
2. Verifies the `zulip` Python package is available via `uv run`
3. Checks for a `.zuliprc` credentials file
4. Tests connectivity to leanprover.zulipchat.com

If `.zuliprc` is missing, the script prints instructions for creating one.
The user will need to get an API key from their Zulip account settings.

After setup, suggest `/zulip` to search for community discussions.
