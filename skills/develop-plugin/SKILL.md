---
name: develop-plugin
description: >-
  Develop, debug, test, or maintain the AutoformBot plugin itself. Use when
  changing autoform_cli/, servers/, skills/, plugin manifests, tests, or the
  bundled formalization example; fixing plugin behavior observed in a Lean
  formalization project; or refreshing a local plugin build. Treat the
  Cabannes thesis repository as an executable consumer example and optimize
  for correct installed behavior in independent formalization projects; do
  not use for doing the mathematics in a consumer project.
---

# Develop Autoform from consumer outcomes

Treat Autoform as an example-based plugin. Its product is the result a user
gets after installing it in a real formalization project. The bundled
`skills/setup/assets/cabannes-thesis-project/` repository is an executable,
representative consumer of that product, not the product itself.

Before editing, inspect the worktree and express the request as a consumer
scenario: given Autoform installed in an independent formalization repository,
what action should produce what result? Trace that result through the relevant
skill, CLI, server, manifest, example, and tests. For an internal refactor,
identify the installed behavior that must stay invariant.

Implement reusable behavior in the plugin. Keep theorem names, source content,
and other Cabannes-specific facts in the example and worked references. Update
the example when it should demonstrate the new outcome, but never special-case
production code for it. Prefer a focused test of the mechanism plus an
acceptance assertion in `tests/test_skill_examples.py`.

Treat plugin and formalization roots as distinct, because skills run from an
installed plugin against another repository. Keep development policy here;
consumer-facing skills should contain only guidance needed to formalize.

Run focused checks while iterating, then normally run:

```bash
make lint
make test
make check-example
```

Run `lake build` in the example when its Lean results change. Validate edited
skills and the manifest with the skill-creator and plugin-creator validators.
When the updated plugin must be exercised through Codex, use the
`plugin-creator` cachebuster and reinstall workflow, then test discovery in a
new thread. Report the consumer outcome, example evidence, and checks.
