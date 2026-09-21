---
name: develop-plugin
description: >-
  Develop or maintain AutoformBot's CLI, servers, skills, manifests, tests,
  bundled example, or local installation. Use for plugin defects seen in
  consumer Lean projects; not for their mathematics.
---

# Develop Autoform from consumer nudges

Treat Autoform as an example-based plugin tested through installed behavior in
an independent formalization repository. The bundled Cabannes thesis is only
an executable consumer example.

Inspect installed behavior in one consumer scenario. For a refactor, name the
invariant and trace its layers.

Treat public stateless Lean execution as disposable by default: one public call
owns one child-process generation, and cleanup finishes before the call returns.
Add cross-call process reuse only after a measured need and an explicit
state-generation protocol.

Treat user nudges as product evidence. Preserve the insight, not the transcript.
Encode reusable triggers, decision rules, and actions so future agents need less steering.
Add a focused assertion in `tests/test_skill_examples.py`.

Keep Cabannes-specific facts in examples and references, never runtime code.

Keep plugin and formalization roots distinct. Agents can infer routine details;
document only non-obvious constraints and fragile steps.

Run focused checks, then normally `make lint`, `make test`, and
`make check-example`. Run `lake build` when example Lean changes. Validate
edited skills and manifests with skill-creator and plugin-creator. Use
cachebuster and reinstall only to test discovery in a new thread.
