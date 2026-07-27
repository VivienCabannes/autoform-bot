# Pilot and stress-test guide

Use a disposable Lean project for live tests. The automated suite never needs
paid credentials or an external network; live Claude, Codex, OpenAI, Avocado,
and Aristotle runs may incur usage and may send the selected project data to
their configured provider.

## 1. Release gate

From the Autoform repository root:

```bash
uv sync --extra dev --extra repl --extra zulip
LC_ALL=C LANG=C uv run python -m pytest -q
uv run ruff check scripts servers tests
python3 scripts/lint_plugin.py
claude plugin validate .
git diff --check
```

When Lean/Lake is installed, the suite also compiles small temporary projects
against the installed toolchain and runs the real kernel axiom probe. Without
Lake, those tests skip. Provider tests use an in-process loopback HTTP server;
they never contact an external model.

The bundled demo is deliberately incomplete, so `lake build` succeeds with
expected `sorry` warnings. A true cold start also downloads the Mathlib cache:

```bash
cp -R examples/demo-project /tmp/autoform-demo-pilot
cd /tmp/autoform-demo-pilot
LC_ALL=C LANG=C lake update
LC_ALL=C LANG=C lake exe cache get
LC_ALL=C LANG=C lake build
```

## 2. Host preflight

Codex:

```bash
codex login status
uv run python scripts/install_host_agents.py install \
  --host codex --project /tmp/autoform-demo-pilot
uv run python scripts/install_host_agents.py check \
  --host codex --project /tmp/autoform-demo-pilot
```

Claude:

```bash
claude auth status
claude plugin validate .
```

The Codex check must report every generated role as `ok` and `changed=0`.
Start a new project-rooted Codex task after installation so native role
discovery sees the new TOML files.

If role discovery is probed through `codex exec`, do **not** use `--ephemeral`:
subagent delegation needs a registered parent thread. Judge the probe from the
JSONL event stream, not from the model's final self-report. Success requires an
actual spawned child id, a non-empty receiver set while waiting, and a terminal
child result. A missing/failed spawn followed by a plausible statement such as
`"used_agent": "autoform_reader"` is a failed pilot, not evidence of delegation.

## 3. API-provider preflight

Configuration-only checks do not send a request:

```bash
uv run python scripts/provider_check.py openai
uv run python scripts/provider_check.py avocado
```

After reviewing the printed provider, model, credential-variable name, and data
policy, the temporary-marker capability probe is:

```bash
uv run python scripts/provider_check.py avocado --live --timeout 30
```

Success requires two bounded Chat Completions turns and at least one real tool
call. It is only a capability check; a real dispatcher run still requires the
explicit `--allow-api-egress avocado` flag.

## 4. Disposable worker and jury pilot

Run setup/plan first so the plan directory has `graph.json` and the selected
node has a graph-pinned `lean_file`. Then enqueue one easy disposable node:

```bash
uv run python scripts/dispatch_queue.py <plan-dir> enqueue \
  --agent worker --node <node-id> --source pilot
```

Codex worker:

```bash
LC_ALL=C LANG=C uv run python scripts/dispatch_runner.py <plan-dir> \
  --repo <lean-project> --workers --backend codex \
  --judge-backend codex --timeout 180 --max-steers 0
```

Claude worker uses `--backend max`. A forced-timeout recovery check uses the
same disposable node with `--timeout 1`; it must fail, mention that the child
was killed, raise exactly one escalation, and leave no host child running.
Claim and resolve that escalation before retrying the node.

For the three-axis jury:

```bash
uv run python scripts/dispatch_queue.py <plan-dir> enqueue \
  --agent reviewer --node <node-id> --source pilot
LC_ALL=C LANG=C uv run python scripts/dispatch_runner.py <plan-dir> \
  --repo <lean-project> --backend codex \
  --judge-backend codex --timeout 120 --limit 1
```

The run is complete only when:

- the queue task is terminal;
- a proved worker has a clean `lake build` and kernel/axiom gate;
- `review_status.json` contains only actual judge scores;
- `.autoform/usage.jsonl` contains the worker backend, status, wall time, and
  usage;
- no `codex exec` or headless `claude -p` child remains; and
- native activity entries in `agents_status.json` survive queue synchronization
  until their owner explicitly clears them.

## 5. Failure drills

Run these only on disposable copies:

- malformed or duplicate queue records must return a state error without
  changing the file bytes;
- two dispatchers for one plan must leave the second one rejected by the
  project lease;
- an unresolved graph dependency must reject the entire merge without a
  partial write;
- a rejected API-written proof must restore the exact previous bytes;
- malformed provider envelopes, duplicate tool-call IDs, oversized arguments,
  and excessive tool fan-out must terminate inside the configured bounds; and
- 24 concurrent queue, graph, and usage-ledger writers must preserve every
  unique record.

These drills are automated in the default test suite. The live steps above
verify host authentication, CLI schema behavior, subscription permissions, and
real Lean-version compatibility that injected tests cannot establish.

Record release-candidate outcomes in a dated result file. The current local
hardening run is [pilot-results-2026-07-27.md](pilot-results-2026-07-27.md);
open gates in that record remain release blockers until a later result
supersedes them.
