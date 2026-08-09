---
name: orchestrate
description: Work through an Autoform Markdown blueprint with native agents and Lean tools.
---

Treat `kind: node` pages under `blueprint/roadmap/**/*.md` and their dependency
links as the source of truth. Follow `internal/runbooks/proving.md` and
`internal/runbooks/review.md` for the preserved detailed worker procedures, and
use `scripts/backend_config.py` for provider selection. Work ready nodes with native subagents plus the
Lean LSP and REPL servers. Record only what compiled: `statement: formalized`,
`proof: formalized`, and the exact declaration name in `lean`; ready and
fully-proved are derived by `autoform check`, so never write them by hand.
Schedule prerequisite nodes before their dependents and parallelize only nodes
whose declared statement or proof prerequisites are satisfied.
Search local Mathlib or community prior art
with host-native tools when useful, then verify every candidate with Lean before
reporting completion.

Roadmap owns initial source decomposition and deliberate DAG revisions. Do not
scan for undecomposed chapters or construct the initial plan here; hand a
planning gap back to Roadmap unless the user explicitly requests that bounded
roadmap repair.

Local Lean success is not the final signal: `autoform-verify.yml` re-validates
the DAG, builds the project, rejects unfinished or unsafe proofs, and audits
axioms on pull requests. When the user works through pull requests, open one
after the affected nodes build locally, then read the outcome instead of
assuming it:

```bash
gh pr create --fill --draft
gh pr checks --watch --fail-fast
run=$(gh run list --branch "$(git branch --show-current)" --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view "$run" --log-failed
```

Treat a red run as evidence about the node rather than about the workflow: fix
the Lean or the frontmatter and push again, and never record
`proof: formalized` while a node's pull request is failing. Opening pull requests and pushing are
outward-facing actions; take them only when asked, and report the commands
instead when `gh` is missing or unauthenticated.

For a concrete source-to-node handoff, consult the concise [Cabannes thesis walkthrough](references/thesis-worked-node.md). It illustrates dependency-based scheduling, not a theorem or declaration to copy.
