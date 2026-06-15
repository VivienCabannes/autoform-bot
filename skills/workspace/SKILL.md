---
name: workspace
description: >
  Inspect a Lean 4 workspace — project structure, sorry/axiom counts,
  declarations, targets, and readiness. Use at the start of any
  formalization session to triage the project.
  Trigger: /workspace, "inspect workspace", "scan project",
  "how many sorry", "project status".
---

# Workspace Inspection

Run the inspection script to get a structured overview of the Lean project:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/workspace/inspect.py" [path]
```

If no path is given, it uses `$LEAN_PROJECT_DIR` or the current directory.

The script reports:
- Lakefile and toolchain version
- Targets file and book/source file locations
- Lean file count and declaration count
- Sorry count and axiom count
- Available tools (lake, lean, rg)
- Recommended next steps

For searching `.lean` files:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/workspace/inspect.py" --search "pattern" [path]
```

For listing declarations:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/workspace/inspect.py" --declarations [path]
```

For reading targets:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/workspace/inspect.py" --targets [path]
```
