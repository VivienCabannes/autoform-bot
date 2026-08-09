---
name: orchestrate
description: Work through an Autoform Markdown blueprint with native agents and Lean tools.
---

Treat `kind: article` pages under `blueprint/roadmap/**/*.md` and their dependency
links as the source of truth. Work ready nodes with native subagents plus the
Lean LSP and REPL servers. Record only what compiled: `statement: formalized`,
`proof: formalized`, and the exact declaration name in `lean`; ready and
fully-proved are derived by `autoform check`, so never write them by hand.
Schedule prerequisite nodes before their dependents and parallelize only nodes
whose declared statement or proof prerequisites are satisfied.
Search local Mathlib or community prior art
with host-native tools when useful, then verify every candidate with Lean before
reporting completion.

Claim a node before working it, so two agents or machines cannot take the same
one. Set a stable identity in `AUTOFORM_WORKER_ID`, then acquire, renew while
the attempt runs, and release when it ends, including on failure or handoff:

```bash
autoform claim acquire "<node-id>"
autoform claim renew   "<node-id>"
autoform claim release "<node-id>"
```

Claims are fail-closed Git-ref leases. A refusal or a claim-board error means
ownership is unproven: pick different work or stop, and never fall back to
working the node unclaimed. If renewal fails or becomes uncertain mid-attempt,
stop before committing or pushing. `autoform claim list` shows the current
holders; a stale lease expires on its own, so do not clear one by hand.

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
