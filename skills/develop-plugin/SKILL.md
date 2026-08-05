---
name: develop-plugin
description: >-
  Develop or maintain AutoformBot's CLI, servers, skills, manifests, tests,
  bundled example, or local installation. Use for plugin defects seen in
  consumer Lean projects; not for their mathematics.
---

# Develop Autoform from consumer outcomes

Treat Autoform as an example-based plugin. Its product is installed behavior in
an independent formalization repository; the bundled Cabannes thesis repository
is only an executable consumer example.

Inspect the worktree, then state a consumer scenario: given installed Autoform,
what action should produce what result? Trace only relevant layers. For a
refactor, name the installed behavior that must stay invariant.

Implement reusable plugin behavior. Keep Cabannes-specific facts in the example
and references; use it to demonstrate outcomes, never special-case it. Prefer a
focused mechanism test and an acceptance assertion in
`tests/test_skill_examples.py`.

Keep plugin and formalization roots distinct and development policy here.
Skills must be succinct. Agents can infer routine details; include only
non-obvious constraints, domain knowledge, and fragile steps.

Run focused checks, then normally run:

```bash
make lint
make test
make check-example
```

Run `lake build` when example Lean results change. Validate edited skills and
the manifest with the skill-creator and plugin-creator validators. Use the
plugin-creator cachebuster and reinstall workflow only to test installed
discovery in a new thread. Report the consumer outcome and checks.
