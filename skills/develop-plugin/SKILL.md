---
name: develop-plugin
description: >-
  Develop AutoformBot's CLI, servers, skills, tests, and installation.
---

# Develop Autoform

Treat Autoform as an example-based plugin installed in an independent formalization repository.
Use the Cabannes thesis as an executable consumer.

Inspect the worktree, state a consumer scenario, observe installed behavior, and name refactor invariants.

Treat user nudges as product evidence. Encode reusable triggers, decisions, and
actions so future agents need less steering. Preserve insight, not the transcript.
Add a focused test and acceptance assertion in `tests/test_skill_examples.py`.

For each supported Lean/Mathlib release, update the catalog, module-root
collision contract, and complete Lake-generated manifest artifact together.
Generate the manifest with `lake update` and run a fresh `lake build`; a manifest
containing only the direct Mathlib package is invalid without inherited dependencies.

Keep Cabannes-specific facts in examples; implement reusable behavior without special cases.

Keep plugin and formalization roots distinct. Agents can infer routine details;
keep skills to non-obvious constraints and fragile domain steps.

Run focused checks, then normally `make lint`, `make test`, and `make check-example`.

Run `lake build` when example Lean results change. Validate edited skills and
the manifest with skill-creator and plugin-creator. Use cachebuster and reinstall
only for installed discovery in a new thread. Report checks.
Treat rewritten private declaration safety as fail-closed evidence: correlate
the official user name to its lexical declaration by source coordinates.
