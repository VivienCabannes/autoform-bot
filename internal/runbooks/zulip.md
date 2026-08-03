# Zulip search runbook

Search the Lean/Mathlib community Zulip before proving or naming anything non-trivial.

## When to search

- **Before naming** a new definition or theorem — check if a convention exists.
- **Before proving** — someone may have discussed the best approach or identified pitfalls.
- **When stuck** — the "Is there code for X?" stream is specifically for this.
- **Before adding a new file** — check if someone already has a PR in progress.

## How to use

Zulip search is stateless and does not need an MCP server. Use the official
Python client on demand through Autoform's thin API helper:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" --extra zulip python -c \
  'from servers.zulip.core import get_client; import json,sys; print(json.dumps(get_client().search_messages(sys.argv[1]), indent=2))' \
  "Hoeffding bound"
```

For a known stream and topic, replace the final expression with
`get_client().get_messages(sys.argv[1], sys.argv[2])` and pass the stream and
topic as two arguments. Use `get_client().list_streams()` to discover streams.
The helper reads `.zuliprc`, calls the Zulip API, and exits after printing JSON.

For credential setup and search examples, read
`<AUTOFORM_PLUGIN_ROOT>/internal/references/zulip-configuration.md`.

## Key streams for Mathlib work

- **mathlib4**: main development discussions
- **Is there code for X?**: ask before building from scratch
- **new members**: beginner questions, often about API discovery
- **general**: cross-cutting topics
- **Autoformalization**: autoformalization projects and tools

## Citing Zulip in code

When a Zulip discussion informed a design choice, add a comment:

```lean
/-- Hoeffding's inequality. See Zulip discussion:
https://leanprover.zulipchat.com/#narrow/stream/mathlib4/topic/Hoeffding -/
theorem hoeffding_bound ...
```
