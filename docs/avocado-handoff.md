# Avocado / muse1.1 backend — work-laptop handoff

Written on a personal laptop **without Meta internal access**. Everything that
could be built and tested from outside is done and on this branch; this doc
lists exactly what remains, every assumption that needs verification, and the
fallback plan if the internal surface has a different shape.

## What is already built (works today, fully tested with fake transports)

- `servers/prover/openai_adapter.py` — a generic **OpenAI-compatible
  request/response prover adapter** (`OpenAICompatAdapter`) behind the standard
  `ProverAdapter` ABC: sends the node spec + no-cheating discipline as one
  chat-completions call, extracts the fenced Lean file from the reply, lands it
  at the plan node's `lean_file` (or a sanitized model-declared `-- FILE:`
  header), and claims `proved` — which the driver's kernel honesty gate then
  independently verifies. An unknown model is therefore safe by construction:
  a false claim is caught by `lake build` + `#print axioms`, never trusted.
- Backend names **`avocado`** and **`openai`** wired into
  `servers/prover/server.py::_make_adapter` (lazy import) — usable as
  `prove_node(..., backend="avocado")`.
- `SteeringCapability.NONE` — the driver never live-judges or gate-folds this
  backend (request/response has no session); a rejected claim downgrades and
  retry happens at the dispatch level.
- Token accounting: `usage.prompt_tokens`/`completion_tokens` from the response
  are captured into `ProofResult.meta["usage"]`, flow into the per-project
  ledger (`.autoform/usage.jsonl`), and roll up into `formalization.yaml`.

## Why the public preset looks the way it does

Public reporting (CNBC 2026-07-09; ai.meta.com; dev.meta.ai) establishes:
**"Avocado" was the internal codename for the model released as Muse Spark**
(Muse Spark 1.1, July 2026). The public surface is the **Meta Model API**:
base `https://api.meta.ai/v1`, model id `muse-spark-1.1`, Bearer auth, drop-in
OpenAI-compatible (Chat Completions + Responses), tool calling, 1M context,
$1.25/$4.25 per Mtok. The `avocado` preset defaults to that public surface —
so the adapter is **already live-testable from any laptop with a public
`MODEL_API_KEY`** — and every internal-specific fact is an env override.

## The fill-in checklist (each item UNVERIFIED until confirmed internally)

Set these on the work laptop; no code changes needed if the internal gateway is
OpenAI-compatible:

| # | Question | Env override | Public default |
|---|----------|--------------|----------------|
| 1 | Internal gateway base URL (does an internal OpenAI-compatible endpoint exist? Devmate's multi-model routing suggests a gateway does) | `AUTOFORM_AVOCADO_BASE_URL` | `https://api.meta.ai/v1` |
| 2 | Auth: plain bearer key? SSO-derived short-lived token? mTLS? Which env var holds it? (headless scripts must be able to obtain it) | `AUTOFORM_AVOCADO_KEY_VAR` = *name* of the var holding the credential | `MODEL_API_KEY` |
| 3 | Internal model id: literally `muse1.1`? `avocado`? tiers? | `AUTOFORM_AVOCADO_MODEL` | `muse-spark-1.1` |
| 4 | Extra required headers (identity/routing headers are common on internal gateways) | `AUTOFORM_AVOCADO_EXTRA_HEADERS` (JSON dict) | none |
| 5 | Wire shape: Chat Completions? Responses-only? bespoke? | (code change if bespoke — see fallback) | Chat Completions |
| 6 | Tool calling available/allowed? (enables the future agent mode) | n/a (sample mode doesn't need it) | yes, public |
| 7 | Rate limits / quotas / acceptable-use for long agent loops | n/a | public preview limits |
| 8 | **Data policy: is sending textbook excerpts + project Lean source to this endpoint approved?** Sources are third-party copyrighted texts — check internal data-handling rules BEFORE first live run | n/a — policy, not code | n/a |

## First live smoke test (10 minutes, work laptop)

```bash
export AUTOFORM_AVOCADO_BASE_URL=...      # from #1
export AUTOFORM_AVOCADO_KEY_VAR=...       # from #2 (the VAR NAME)
export AUTOFORM_AVOCADO_MODEL=...         # from #3
uv run --frozen --with pytest python3 -m pytest tests/test_openai_adapter.py -q   # still green
python3 - <<'EOF'
from servers.prover.server import run_prove_node
r = run_prove_node(graph_path="<plan>/graph.json", node_id="<easy node>",
                   project_dir="<lean project>", backend="avocado")
print(r.status, r.reason, r.meta.get("usage"))
EOF
```

A trivial node proved end-to-end (gate-verified) + a sane token count in the
ledger = the backend is real. Then check `.autoform/usage.jsonl` and
`formalization.yaml` picked up the run.

## Fallback plans, by discovered shape

- **Internal gateway is Responses-API-only**: add a `payload_style="responses"`
  branch in `OpenAICompatAdapter.events()` (~30 lines: different payload +
  `output[0].content[0].text` extraction). Everything else unchanged.
- **Internal surface is a CLI** (a Devmate-style headless exec): port the
  Codex adapter — `_cli_common.py` already owns the spec prompt, discipline
  skeleton, JSONL parse, env scrub, deadline-enforcing subprocess runner, and
  the FAILED verdict parse. ~250 lines, ~half a day; declare
  `SteeringCapability.BETWEEN_TURNS` if it supports a resume verb.
- **Internal surface is a Python SDK**: mirror `aristotle_adapter.py` (lazy
  import, injectable fake lib, sync ABC surface over an async core).

## Two-minute live probes to run on ANY laptop with claude/codex access

- **Claude cost accounting (VERIFY-LIVE, flagged in claude_adapter.py):** the
  adapter SUMS each turn's `total_cost_usd`/`usage` across `--resume` turns on
  the reasoning that each `claude -p` invocation reports its own turn. Verify:
  `claude -p "hi" --output-format json` (note cost/usage), then
  `claude --resume <session_id> -p "hi again" --output-format json`. If the
  second figure is its own tiny turn → the code is correct; if it's roughly
  first + tiny → it's session-cumulative and the adapter must diff, not sum.
- **Codex usage events:** confirm on one `codex exec --json` transcript that
  usage dicts are per-event deltas, not cumulative snapshots (the adapter sums
  every usage dict it sees).

## Known limitations (documented, deliberate scope cuts)

- **[FIXED] A failed avocado/openai run overwriting an existing target file.**
  Backup/restore has landed. `_land` still writes before verification (the gate
  needs the file on disk), but it now records the target's pre-land bytes in
  `meta["landed_backup"]`, and the driver restores them when the honesty gate
  rejects the claim — rewriting the prior content, or deleting a file the run
  newly created (`servers/prover/driver.py::_restore_landed`, keyed off the meta
  contract not the backend name, so any request/response adapter gets it for
  free). The backup lives in memory only and is popped before the ledger sees the
  result; the tool response surfaces `landed_restored`. Covered by
  `tests/test_openai_adapter.py::test_clobber_*`. Residual: the stale `.olean`
  from the rejected build is left for the next `lake build` to recompile, so
  running against committed trees / worktree-per-worker (proposal #7) is still
  the cleanest isolation.

## Future work (explicitly out of scope on the personal laptop)

- **Agent mode** (tool loop via chat-completions tool-calling, proposal #4's
  `mode: agent`) — Muse Spark's headline capability; needs internal tool-call
  schema confirmation (#5/#6) first.
- Multi-sample compile-check loop (request n candidates, REPL-check each,
  land the first that compiles) — needs the REPL server wired in-adapter.
- Registry entry in the proposal-#4 `backends.yaml` once that lands (today the
  preset lives in `_PRESETS` + `_make_adapter`).
