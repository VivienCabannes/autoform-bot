# Routing inference through a proxy (e.g. a Claude Max subscription)

By default the Anthropic backend authenticates with `ANTHROPIC_API_KEY` and bills per token
against the Anthropic API. You can instead point it at a **proxy** that supplies its own
credentials — for example a local proxy that authenticates against a Claude **Max** subscription,
so a run bills against the subscription rather than per token.

This is configured entirely through the environment; no code or `config.yaml` changes are needed.

## Environment variables

| Variable | Effect |
|---|---|
| `ANTHROPIC_BASE_URL` | Overrides the Anthropic base URL for every Anthropic model. Point it at your proxy (e.g. `http://localhost:4000`). |
| `ANTHROPIC_AUTH_TOKEN` | Sends `Authorization: Bearer <token>` instead of an `x-api-key` header. |
| `ANTHROPIC_API_KEY` | Still honored and takes precedence. **No longer required** once `ANTHROPIC_BASE_URL` or `ANTHROPIC_AUTH_TOKEN` is set. |

When either `ANTHROPIC_BASE_URL` or `ANTHROPIC_AUTH_TOKEN` is set, `create_inference`
(`core/inference/client.py`) no longer raises "Set ANTHROPIC_API_KEY" — the proxy/bearer endpoint
is expected to provide the credentials.

## Usage

```bash
# Point the engine at a proxy that injects your Max OAuth.
export ANTHROPIC_BASE_URL=http://localhost:<proxy-port>
# Optional: most Max proxies ignore the key; some want a bearer token instead.
# export ANTHROPIC_AUTH_TOKEN=<token>

python -m autoform.bot.main run --config=path/to/config.yaml --name=my-run --fresh
```

Every Anthropic model (`Opus 4.6`, `Sonnet 4.6`, `Haiku 4.5`, …) is routed, so the whole
multi-agent pipeline — including the tool-using `worker` / `orchestrator` / `judge` / `matcher`
agents — runs through the proxy. This works because the proxy still speaks the Anthropic Messages
API, so the external agent loop and its MCP tool-use are preserved unchanged.

## What you need to supply

A proxy that:

1. accepts Anthropic Messages API requests at `ANTHROPIC_BASE_URL`, and
2. forwards them to Claude authenticated with your Max session (OAuth).

The proxy is **not** included here — bring your own (e.g. an Anthropic-compatible passthrough that
holds a Claude Code / Max login). Without `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` the backend
behaves exactly as before.

## Caveats

- **Terms of service.** Routing non–Claude-Code traffic through a Max subscription depends on your
  proxy and your agreement with Anthropic. Use a proxy you trust; this repository only makes the
  base URL / auth token configurable.
- **Cost numbers are notional under a subscription.** Token costs are still computed from each
  model's per-token `ModelPricing` for tracing and budgets; under a Max proxy those dollar figures
  are estimates, not real charges.
- **A bare `ANTHROPIC_AUTH_TOKEN` against `api.anthropic.com` will not work** for Max OAuth tokens
  — Anthropic gates OAuth tokens to the Claude Code client. That is exactly what the proxy
  (`ANTHROPIC_BASE_URL`) is for.
