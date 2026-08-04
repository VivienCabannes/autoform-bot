# Distributed worker runbook (the `autoform` CLI)

Internal operating material for Orchestrate's distributed mode. Not a user
command surface — the user-facing workflows remain setup / orchestrate /
set-backend. Design contract: `docs/worker-cli.md`.

## When distributed mode applies

The Lean project has a GitHub `origin` remote AND the user asked for
multi-machine/team progress (or a worker loop). Single-machine orchestration is
unchanged; the CLI *adds* the cross-machine layer, it does not replace the
local engine.

## Invocation

Always through the plugin root (same resolution ritual as every skill):

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" \
  python -m autoform_worker <subcommand> --project "$DISPATCH_PROJECT" ...
```

(`"<AUTOFORM_PLUGIN_ROOT>/autoform" <subcommand>` is the equivalent shim.)

## Preflight — before the first round

```bash
python -m autoform_worker doctor --json
```

Every failing check must either be fixed or explicitly accepted by the user
(e.g. "issues disabled — escalation sync degrades to local"). Never start a
loop on a failing `gh auth`, missing `origin`, or dirty working tree.

## The round contract

`work` runs ONE unit: survey → first actionable stage → execute → one visible
GitHub mark. Stage cascade (fixed): `rebase → fix-ci → fix → review → progress
→ prove`. Exit 0 = progressed, 75 = nothing actionable (normal), 1 = error.

- `--dry-run` surveys and reports without executing — use it to show the user
  what would happen.
- `--only`/`--skip` restrict stages (e.g. `--only review,progress` for a
  review-only machine).
- `--backend` overrides the persisted set-backend choice for prove/fix work;
  unknown names fail closed exactly like the dispatcher.
- API providers (`openai`/`avocado`) require the same per-process consent as
  the dispatcher: run `scripts/provider_check.py`, obtain explicit user
  approval, then pass `--allow-api-egress <provider>`. Never pass it without
  fresh approval.

`work --loop` runs rounds forever (detached, like the dispatch watch process):

```bash
nohup uv run --directory "<AUTOFORM_PLUGIN_ROOT>" \
  python -m autoform_worker work --loop --project "$DISPATCH_PROJECT" \
  >> "$DISPATCH_PROJECT/worker.log" 2>&1 &
```

Reuse an existing loop (check `pgrep -f "autoform_worker work --loop"`); never
run two loops for one worker id.

## Claims — cooperative leases, CAS safety

- A claim is a git ref lease (`refs/autoform-claims/<key>`) in the canonical
  repo; `acquire`/`renew`/`release`/`holds`/`list`/`gc` via `autoform claim`.
- Keys: `author/<slug>-<hash>` for proving a node (node ids are free text, so
  the key is DERIVED — always use `autoform claim acquire --node "<node id>"`
  rather than hand-building it), `branch/<pr>` (rewriting a PR head),
  `progress` (the fold/publish unit).
- Claims are cooperative. The board being down NEVER blocks work — log it and
  continue; safety is the CAS push layer (`autoform push` /
  `gitutil.safe_push`), which refuses any branch write whose observed remote
  OID went stale.
- Before manually proving a node in a distributed project (engine or native
  subagent path), run `autoform claim acquire --node "<node id>"` first and
  release it when the PR opens or the attempt ends. If the acquire reports
  "held by a live peer", pick different work — never race a peer on the same
  node.
- Never `--steal` without explicit user instruction.

## Review state across machines

- The jury verdict for a PR lives ON the PR: a comment carrying
  `<!--autoform-scoreboard-->` + `<!--autoform-meta:v1 {...}-->`, scoped to the
  head SHA. The committed `review_status.json` is updated only by the
  `progress` unit folding *merged* PRs' scoreboards (deterministic and
  idempotent — every machine converges).
- `autoform sync` folds merged scoreboards into the LOCAL sidecar without
  pushing — run it at orchestration start in distributed projects so local
  dashboards reflect merged reality.
- Human verdicts recorded in the local dashboard remain immutable; folds only
  write the `ai` slot.

## Escalations ↔ GitHub issues

When the canonical repo has Issues enabled, `progress` (or `autoform issues
sync`) mirrors open engine escalations to issues labeled `autoform:escalation`
and closes resolved ones. Humans register intent with assigned
`autoform:intention` issues titled `intention: <node-id>`; assigned intentions
join every worker's prove avoid-list. With Issues disabled, both degrade to
local-only — say so rather than silently losing the sync.

## PR discipline

- Prove PRs: branch `autoform/<node-slug>-<worker-id>-<stamp>`, body carries
  the `autoform-target:v1` marker, verification statement, and backend
  attribution. Opened via the fork when the operator lacks push access.
- Agent-driven pushes go ONLY through `autoform push` (CAS + lease check) and
  PRs through `autoform pr-create` (marker + lease gate). Raw `git push` from
  a spawned agent is a defect.
- Merging stays human (or explicit repo policy) — the worker never merges.
