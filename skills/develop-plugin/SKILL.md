---
name: develop-plugin
description: >-
  Develop or maintain AutoformBot's CLI, servers, skills, manifests, tests,
  bundled example, or local installation. Use for plugin defects seen in
  consumer Lean projects; not for their mathematics.
---

# Develop Autoform from consumer nudges

Autoform is an example-based plugin installed in an independent formalization
repository. Use the bundled Cabannes thesis as its executable consumer example.

Inspect the worktree through a consumer scenario. For refactors, name the
invariant and trace needed layers.

Treat user nudges as product evidence. Distill them into the owning skill as a
decision rule and action.
Ensure future agents need less steering.
Preserve the insight, not the transcript.
Add a focused test and acceptance assertion in `tests/test_skill_examples.py`.

Implement reusable plugin behavior. Keep Cabannes-specific facts in the example
and references; demonstrate outcomes without special-casing them.

Fence every dispatched REPL response against the project configuration generation;
scrub ambient Lean/Lake overrides and permit only initial manifest materialization.

Keep plugin and formalization roots distinct. Agents can infer routine details;
keep skills to non-obvious constraints and fragile domain steps.

Run focused checks, then run:

```bash
make lint
make test
make check-example
```

Run `lake build` when example Lean results change. Validate edited skills and
the manifest with skill-creator and plugin-creator. Use cachebuster and reinstall
only to test installed discovery in a new thread. Report outcome and checks.
