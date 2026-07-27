---
name: set-backend
description: >-
  Show or persist the Autoform proof-worker backend and its billing/data path.
  Use when the user asks to choose, change, inspect, or configure Claude Max,
  Aristotle, Codex, OpenAI, or Avocado as the prover.
---

# Set the proof-worker backend

Resolve an absolute plugin root from a valid `AUTOFORM_PLUGIN_ROOT`,
`PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
`Path(<this loaded SKILL.md>).resolve().parents[2]`. Substitute that absolute
path into each command.

Use:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/backend_config.py" list
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/backend_config.py" set <backend>
```

Supported user-facing backends:

- `max`: Claude CLI authenticated by the user's Max session.
- `aristotle`: Harmonic Aristotle via `ARISTOTLE_API_KEY`.
- `codex`: Codex CLI using its configured OpenAI/ChatGPT authentication.
- `openai`: an OpenAI-compatible endpoint, configured by
  `AUTOFORM_OPENAI_*`.
- `avocado`: Meta's OpenAI-compatible deployment, configured by
  `AUTOFORM_AVOCADO_*`.

With no requested backend, show the list and current `*` selection. With a
backend, persist it and report the exact prover adapter, credential variable or
host authentication, billing path, and whether project content may leave the
machine.

For `openai` or `avocado`, also run:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/provider_check.py" <backend>
```

This is configuration-only unless the user asks for `--live`. Persisting an API
backend does not constitute consent to send project data; `orchestrate` obtains
per-project/run approval before a real workload.

Never infer an unknown backend as Claude. Let validation fail closed.
