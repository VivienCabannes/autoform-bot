# TODO

## Branch integration: Deicyde/main + Vivien main

- [x] Unify durable state: keep `graph.json` as the control plane and add an
  authored `wiki/` knowledge base with deterministic generated navigation.
- [ ] Reconcile setup around one inspected ideal repository shape, without a
  bundled worked-example repository.
- [ ] Reconcile roadmap planning, graph review, and source-coverage behavior.
- [ ] Reconcile orchestration, proof escalation, jury review, and backend choice.
- [ ] Reconcile local dashboard and read-only GitHub Pages publication.
- [ ] Reconcile worker/distributed execution and GitHub collaboration contracts.
- [ ] Finish Claude, Codex, and Muse parity checks and plugin lint coverage.
- [ ] Remove superseded compatibility code after active projects migrate to v4.

- Test installation instruction for Claude Code, Codex and others
- Extend the skills and commands

## installation
- Ensure installation instructions are clear
- Use `uv` to create an environment to run the MCP, try to avoid using MCP for tools that are already native to AI coding agents, or can be implemented through text file for examples
- Ensure the plugin works with Claude, Codex, and later Cursor, ...

# Skils and Commands
## Before a project
- [ ] Setup a Lean project repo
    - Tentatively use the one at https://github.com/leanprover-community/LeanProject
- [ ] Find relevant content on Zulip
    - [ ] Debug the Zulip MCP, how to make sure "zulip Python" library is installed
- [Charles] Map a curriculum of results
- [ ] Find relevant Mathlib infra to map what is missing and create a blueprint

## During a project
- [ ] Formalize incrementally in a DAG fashion
- [ ] Specialized agents for generation
- [ ] Specialized agents for reviewing
- [ ] Specialized agents for maintenance and triage
- [ ] Help humans review the process all along

## After a project
- [ ] Golf proofs
- [ ] Find connections to other formalized results
- [ ] Ensure things are scalable
- [ ] Reflect on the project and create generalizable take-aways for other formalization projects
