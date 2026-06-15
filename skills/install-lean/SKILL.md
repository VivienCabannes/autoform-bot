---
name: install-lean
description: >
  Install Lean 4, elan, and lake. Checks prerequisites, installs the toolchain
  manager, and verifies the install. Use when starting from scratch or when
  lean/elan/lake commands are not found.
  Trigger: /install-lean, "install lean", "setup lean", "elan not found",
  "lean not found".
---

# Install Lean 4

Run the install script:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/install-lean/install-lean.sh"
```

The script is idempotent — safe to re-run. It:

1. Checks platform prerequisites (Xcode CLI tools on macOS, git/curl everywhere)
2. Installs **elan** (the Lean version manager) if not present
3. Installs the default Lean toolchain if `lean` is not on PATH
4. Verifies `lean --version` and `lake --version` both work

After install, suggest `/setup-project` to create a new formalization project.
