# Lean installation runbook

Resolve an absolute plugin root from a valid host variable or
`Path(<this loaded SKILL.md>).resolve().parents[2]`.

Run the install script:

```bash
bash "<AUTOFORM_PLUGIN_ROOT>/scripts/install_lean.sh"
```

The script is idempotent — safe to re-run. It:

1. Checks platform prerequisites (Xcode CLI tools on macOS, git/curl everywhere)
2. Installs **elan** (the Lean version manager) if not present
3. Installs the default Lean toolchain if `lean` is not on PATH
4. Verifies `lean --version` and `lake --version` both work

After installation, continue the Setup workflow to create or initialize the
formalization project.
