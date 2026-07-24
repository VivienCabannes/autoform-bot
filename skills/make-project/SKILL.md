---
name: make-project
description: >
  Set up a new Lean 4 + Mathlib formalization project from the LeanProject
  template. Clones, renames, fetches Mathlib cache, and builds. Use when
  starting a new formalization project.
  Trigger: /make-project, "new lean project", "create project",
  "start formalization", "setup project".
---

# Set Up a Lean 4 Formalization Project

Ask the user for a **project name** (UpperCamelCase, e.g. `ConvexBodies`, `PrimeGaps`) and optionally a target directory, then run:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/make-project/make-project.sh" <ProjectName> [target-dir]
```

The script:

1. Clones the [LeanProject](https://github.com/leanprover-community/LeanProject) template
2. Runs `customize_template.py` to rename `Project` → `<ProjectName>` everywhere
3. Fetches Mathlib cache (`lake exe cache get` — ~2 GB download)
4. Runs `lake build` to verify

**Prerequisites:** git, python3, lean/lake. If `lake` is not found, suggest `/install-lean` first.

After setup, suggest `/autoform-extract` to identify formalization targets from source material.
