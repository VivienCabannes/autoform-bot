# The Autoform Worker CLI (`autoform`)

*The distributed-coordination layer: how many machines advance one roadmap.*

Modeled on Kim Morrison's TauCetiWorker (`kim-em/TauCetiWorker`), which coordinates
AI workers advancing the TauCeti library. This document is the design contract; the
implementation lives in `autoform_worker/`.

## Problem

Everything in Autoform today is single-machine: `fslock` advisory locks, one
`dispatcher.lock` lease per project, a local `task_queue.json`. Multiple people
running Autoform against the *same* formalization project (a shared GitHub repo
holding `graph.json`, `informal_content/`, `kernel/`, `review_status.json`, and the
Lean sources) would collide: two machines proving the same node, review state
diverging per clone, no shared progress view.

## Answer — GitHub is the coordination substrate

Three mechanisms, layered exactly as TauCeti layers them (its `COORDINATION.md`
distinguishes `[HARD]` safety from `[COOP]` throughput):

1. **[HARD] Compare-and-swap branch writes.** Every push the worker makes is
   `git push --force-with-lease=<ref>:<observed-oid>` — GitHub's atomic ref update
   guarantees exactly one writer wins. An empty expected OID means *create-only*.
   Nothing the cooperative layer gets wrong can corrupt a branch.
2. **[COOP] Git-ref lease claims.** A claim is a custom ref
   `refs/autoform-claims/<key>` in the claims repo pointing at an orphan commit
   (empty tree) whose commit message is the lease JSON. Acquire/renew/release are
   all CAS pushes of that ref, so claims inherit git's atomicity — and work on any
   git host with **no server-side setup and no Issues requirement** (GitHub forks
   have Issues disabled by default; this survives that). Claims are *cooperative*:
   losing one never corrupts anything — it only means someone may duplicate work.
   Claim errors fail open with a warning, never block a round.
3. **[COOP] Scoreboard comments.** Review verdicts for a PR live *on the PR* as a
   comment carrying `<!--autoform-scoreboard-->` plus machine-readable
   `<!--autoform-meta:v1 {...}-->` JSON (head-SHA-scoped). The committed
   `review_status.json` sidecar is updated deterministically from merged PRs'
   scoreboards by the `progress` unit — so every clone converges to the same
   sidecar without merge conflicts.

### Lease schema

```json
{"schema": "autoform-claim/v1", "owner": "<worker-id>", "host": "<hostname>",
 "pid": 12345, "acquired_at": 1690000000, "expires_at": 1690001500,
 "resource": "<key>", "note": ""}
```

Keys: `author/<slug>-<sha1[:8]>` for proving a node (derived from the free-text
node id — `autoform claim acquire --node "<id>"` derives it canonically),
`branch/<pr>` (rewriting a PR head), `progress` (the global progress/publication
unit). TTL 1500 s, heartbeat renewal every 300 s while a unit runs; an expired
lease may be taken over. The claims repo defaults to the project's canonical repo
(`AUTOFORM_CLAIM_REPO` overrides — point it anywhere all collaborators can push,
e.g. one member's fork).

## Work units

One round = one survey pass + at most one executed unit, first actionable stage
wins (TauCeti's cascade). Detection is a single pass over `gh pr list --json` +
the local graph/sidecar + the claim board.

| Stage | Candidate | Execution |
|---|---|---|
| `rebase` | own open PR with `mergeable == CONFLICTING` | host agent (claude/codex) with `prompts/rebase.md`; pushes via CAS |
| `fix-ci` | own open PR with failing checks | host agent with `prompts/fix-ci.md` |
| `fix` | own open PR whose scoreboard (at head) has a blocking verdict | host agent with `prompts/fix.md` |
| `review` | open non-draft autoform PR, checks green, head not scoreboarded | deterministic: checkout head, run the 3-axis jury (`judge_runtime`), post scoreboard |
| `progress` | merged autoform PRs newer than the last fold, or stale site export | deterministic: fold scoreboards → `review_status.json`, re-export the static dashboard, sync escalation issues, CAS-push |
| `prove` | eligible graph node: tier-2, not in Mathlib, unproved/defective, prerequisites trusted, unclaimed, no open PR for it | claim `author/<node>`, branch, reuse `dispatch_runner.run_worker()` (spec build + prover driver + verification gate + usage ledger), commit, CAS-push, `pr create` with target marker |

`prove` is deliberately last: it is the expensive, work-generating stage; tending
existing PRs always comes first. Every mutating stage must leave a visible GitHub
mark (new head OID, new comment, or new PR) — a round that claims success without
one exits `75` (no-progress), TauCeti's guard against silent burn.

Attempt budgets (per PR / per node, persisted in worker state): fix 3, fix-ci 3
per head + 5 per PR, rebase 3, review errors 3, prove 3 per node. Provider-infra
failures (5xx/429/transport) refund the attempt rather than burning it.

## PR conventions

- Branch: `autoform/<node-slug>-<worker-id>` from the canonical default branch.
- Fork-first: `ensure_fork()` finds or creates the operator's fork when they lack
  push access; PRs open with `--head <fork-owner>:<branch>`.
- Body must carry `<!--autoform-target:v1 {"node": "<node-id>"}-->` — the
  machine-readable link from PR to graph node (duplicate detection, scoreboard
  folding, avoid-lists). `autoform pr-create` refuses to open a PR without a
  valid marker or with a lost author lease — the `gh-safe-pr-create` semantics.
- Footer: `🤖 Prepared with <backend> via autoform worker`.
- Labels (best-effort): `autoform`, `autoform:prove` etc.

## Trust model

Comments and PRs on a public repo are attacker-writable, so every decision
input is authenticated:

- **Scoreboard metas and in-progress markers** count only when their comment's
  author is trusted — the operator, a declared extra identity, or a repo
  collaborator (API-checked, cached). A forged meta from a drive-by commenter
  is ignored by the survey AND by folding; metas must also match the PR's head
  SHA. Interpolated note text is sanitized (`<!--` neutralized) and the machine
  marker is always the last in its comment, so notes cannot smuggle a second
  marker that overrides the verdict.
- **Reviewing runs the PR's code** (`lake build` on the head). By default only
  PRs from trusted authors are reviewed; `--review-foreign` opts in after a
  human has looked at the diff.
- **Graph fields are data, not paths**: `lean_file`/`content` are followed only
  inside the repo/project roots.
- **Fix agents run isolated**: repo-controlled Claude settings, hooks, slash
  commands, and MCP servers are disabled (`--setting-sources user`,
  `disableAllHooks`, `--strict-mcp-config`), with an allowlisted tool surface;
  `AUTOFORM_UNSAFE_FULL_ACCESS=1` is the explicit opt-out.
- **API egress consent** covers judges too: an `openai`/`avocado` judge backend
  refuses to start without `--allow-api-egress <provider>`, exactly like the
  dispatcher's prover gate.

## Review flow

`review` checks out the PR head **in the operator's own clone** (incremental
`.lake` reuse; a fresh worktree would mean a cold Mathlib build), announces an
in-progress marker comment (`<!--autoform-review-in-progress {...}-->`, TTL) so
peer reviewers skip that head, runs the same jury the local engine runs
(`judge_runtime.run_judge` × faithfulness / proof_integrity / code_quality),
computes the verdict with `review_model.jury_verdict`, and posts the scoreboard.
Verdicts land in the committed sidecar only when the PR merges (via `progress`).

## GitHub issues

When the project repo has Issues enabled, `progress` (and `autoform issues sync`)
mirrors open engine escalations from `task_queue.json` to issues labeled
`autoform:escalation` (title `escalation: <node-id>`, body = the worker's own
words + machine marker), and closes issues whose escalations resolved. Humans can
also claim intent by assigning themselves issues labeled `autoform:intention`;
assigned intentions join the prove avoid-list. Both degrade to no-ops (with a
doctor note) when Issues are disabled.

## CLI surface

```
autoform work   [--loop] [--only S1,S2] [--skip S1] [--backend B] [--dry-run]
                [--project DIR] [--worker-id ID] [--allow-api-egress P]...
autoform status [--json]            # survey + claims + quota-free state snapshot
autoform claim  acquire|renew|release|holds|read|list|gc [key] [--ttl N] [--steal]
autoform push   <ref> [--expect OID] [--remote URL]     # CAS push (git-safe-push)
autoform pr-create ... --body-file F                    # marker+lease-gated gh pr create
autoform sync   [--json]            # fetch + fold merged scoreboards locally (no push)
autoform issues sync [--dry-run]    # escalations <-> GitHub issues
autoform dashboard export|serve     # thin wrappers over the existing scripts
autoform doctor [--json]            # environment + auth + repo capability audit
```

Exit codes: `0` progress, `75` genuine no-progress (loop backs off), `1` error,
`130`/`143` interrupted/terminated. `work --loop` runs rounds forever with
exponential backoff (30 s → 15 min) on failures and a hard per-round timeout
enforced by process-group kill.

## What the CLI reuses (and never re-implements)

- `scripts/dispatch_runner.run_worker` — spec building, prover driver,
  verification gate, usage ledger. The prove unit is a *caller*, not a fork.
- `scripts/review_ui/review_model` — sidecar load/save, `jury_verdict`,
  `verdict_of`, `is_trusted`, trust frontier.
- `scripts/judge_runtime` — the provider-neutral jury.
- `scripts/backend_config` — backend selection; unknown backends still fail closed.
- `scripts/export_github_dashboard` + `configure_github_pages` — publication,
  still gated on committed inputs and explicit approval.
- `scripts/dispatch_queue` — the local queue/feed; worker rounds surface
  themselves in the live dashboard feed via `agent-start`/`agent-done`.
- API egress consent: the CLI forwards `--allow-api-egress` per provider per
  process, exactly like the dispatcher; it never persists or infers consent.

## Non-goals (v1)

- Subscription quota pacing (TauCeti's `quota.py` OAuth-usage model) — the loop
  has backoff and budgets, not usage-endpoint pacing. Planned follow-up.
- A Textual TUI — `status --json` is the machine surface; the dashboards remain
  the human surface.
- `workers.toml` persistent-worker supervision — run `work --loop` under the
  supervisor of your choice; a manager is a planned follow-up.
- Sandboxed rounds (TauCeti `--bubble`) — Autoform's allowlisted agent flags are
  the current containment story.

## Multi-machine walkthrough

Machine A (Vivien) and machine B (Jack) both clone `org/formal-project`, both run
`autoform work --loop`. A's survey finds nodes `alpha`,`beta` eligible; B's finds
the same. A claims `author/alpha` (ref CAS — B's later acquire of `alpha` returns
"held", B takes `beta`). Both prove, push branches to their forks, open PRs with
target markers. B's next round picks stage `review` for A's PR (green CI, no
scoreboard), runs the jury, posts the scoreboard: `clean`. A human (or auto-merge
policy, later) merges. Either machine's next `progress` round folds the merged
scoreboard into `review_status.json`, re-exports the static dashboard, pushes —
the other machine's `sync` fast-forwards. The graph advanced by two nodes with
zero human coordination beyond the merge click.
