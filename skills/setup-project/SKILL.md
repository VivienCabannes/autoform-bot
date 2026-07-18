---
name: setup-project
description: >
  Set up a new Lean 4 + Mathlib formalization project from the LeanProject
  template. Clones, renames, fetches Mathlib cache, and builds. Use when
  starting a new formalization project.
  Trigger: /setup-project, "new lean project", "create project",
  "start formalization", "setup project".
---

# Set Up a Lean 4 Formalization Project

Ask the user for a **project name** (UpperCamelCase, e.g. `ConvexBodies`, `PrimeGaps`) and optionally a target directory, then run:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/setup-project/setup-project.sh" <ProjectName> [target-dir]
```

The script:

1. Clones the [LeanProject](https://github.com/leanprover-community/LeanProject) template
2. Runs `customize_template.py` to rename `Project` → `<ProjectName>` everywhere
3. Fetches Mathlib cache (`lake exe cache get` — ~2 GB download)
4. Runs `lake build` to verify
5. Seeds **`formalization.yaml`** — the [mathlib-initiative self-reporting
   manifest](https://github.com/mathlib-initiative/formalization.yaml) (v0.3) —
   with the project name, git author, and an `automation.methods` entry for
   autoform. From then on every prover run appends token/cost/wall-time data to
   `.autoform/usage.jsonl` and auto-refreshes the manifest's machine-owned
   fields, so the reported models, spend, token totals, and sorry counts stay
   accurate without manual bookkeeping. Human-owned fields (sources, review,
   fidelity, …) are never touched — remind the user to fill in the `sources:`
   TODO entries and the license.

**Existing project?** Seed the manifest alone with:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/formalization.py" init <project-dir>
```

(and `… update <project-dir>` to refresh it manually at any time — any
already-accumulated `.autoform/usage.jsonl` ledger is rolled up on init, so a
late opt-in backfills accurate totals).

**Prerequisites:** git, python3, lean/lake. If `lake` is not found, suggest `/install-lean` first.

After setup, suggest `/autoform-extract` to identify formalization targets from source material.
