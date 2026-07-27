# Avocado provider handoff

Autoform treats Avocado as a private OpenAI-compatible **model provider**, not
as an interactive agent host. Claude Code or Codex can orchestrate the durable
workflow; the deterministic dispatcher can use Avocado for jury or proof work
through Autoform's local Chat Completions tool loop.

## What is implemented

- Explicit `avocado` backend selection in the prover and dispatcher.
- A bounded function-calling loop with project-rooted file reads, listing,
  fixed-string search, allowlisted Lean commands, and one graph-pinned Lean
  write target for prover runs.
- Read-only tools for jury runs.
- Turn, wall-clock, input, output, and write-size limits.
- No shell evaluation of model-supplied command strings.
- The same `ProofResult`, usage ledger, escalation policy, and Lean
  build/forbidden-token/axiom verification gate as every other backend.
- Byte-exact restoration of the pre-run target whenever an API-written
  candidate is not accepted, including an honest failure or verification
  rejection.
- Sample fallback for endpoints that return a complete fenced Lean file but do
  not perform tool calls.

All automated tests use injected transports; CI sends no network request and
requires no credential.

## Required private configuration

Autoform deliberately does not guess a private deployment's endpoint or model
id. Set:

```bash
export AUTOFORM_AVOCADO_BASE_URL="https://<approved-gateway>/v1"
export AUTOFORM_AVOCADO_MODEL="<approved-model-id>"
export AUTOFORM_AVOCADO_KEY_VAR="<NAME_OF_CREDENTIAL_ENV_VAR>"
export NAME_OF_CREDENTIAL_ENV_VAR="<secret>"
```

Optional routing/identity headers are a JSON object:

```bash
export AUTOFORM_AVOCADO_EXTRA_HEADERS='{"X-Route":"avocado"}'
```

`AUTOFORM_AVOCADO_KEY_VAR` contains the credential variable's **name**, never
the credential itself. Autoform reads the secret at request start and does not
place it in prompts, event logs, queues, or usage ledgers.

Public Meta material confirms that Meta offers an OpenAI-SDK-compatible model
API. It does not, by itself, establish a private Avocado URL, model identifier,
authentication method, tool-call support, rate limit, or data policy. Those are
deployment facts to verify internally.

## Preflight checklist

Confirm all of the following before a live formalization:

1. The gateway implements `POST <base>/chat/completions`.
2. Responses use `choices[].message.content` and, for agentic mode,
   `choices[].message.tool_calls`.
3. Tool results are accepted as `role: "tool"` messages with a
   `tool_call_id`.
4. The selected model is permitted to use function tools.
5. The credential and any extra headers are available to a headless process.
6. Long multi-turn runs fit the provider's request, token, and rate limits.
7. Sending the relevant Lean source and mathematical source excerpts to the
   endpoint is approved. The tool loop sends only content the model requests,
   but that content still leaves the local process.

If tool calls are unsupported, set `mode="sample"` when constructing
`OpenAICompatAdapter`, or request multiple samples. Sample mode is useful
compatibility but not agentic parity.

## Safe live smoke

First run the no-network suite:

```bash
uv run python -m pytest -q \
  tests/test_api_tools.py tests/test_openai_adapter.py tests/test_judge_runtime.py
```

Then use a temporary Lean project and an easy disposable node. A live proof is
successful only if:

- the API loop writes the intended graph-pinned file;
- the independent Lean gate accepts it;
- `.autoform/usage.jsonl` records backend `avocado`;
- no secret appears in logs;
- a deliberately rejected candidate restores the original file.

Do not use a third-party textbook or private repository for the first probe.

## Unsupported wire shapes

- A Responses-only deployment needs a Responses transport adapter; do not point
  the Chat Completions client at it and hope the envelopes happen to match.
- A private CLI should get a CLI adapter modeled on `codex_adapter.py`, including
  a documented resume/event contract.
- A private SDK should get a lazy, injected adapter modeled on
  `aristotle_adapter.py`.

In every case, keep provider-specific parsing behind the adapter boundary and
retain the shared verification gate.
