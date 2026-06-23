# Quickstart

Get Autoform running and start playing with it in a few minutes. For the full
vision and command reference see [docs/usage.md](docs/usage.md); for what's
implemented vs. stubbed see the table in [README.md](README.md).

## 0. Play right now (no install)

The workspace scanner is pure-Python (stdlib only), so you can try it before
installing anything:

```bash
make demo
```

This scans the bundled sample project at `examples/demo-project/` and prints its
structure, declarations, and the formalization-target DAG (`targets.yaml`).
Point it at your own project with:

```bash
python3 skills/workspace/inspect.py /path/to/your/lean-project
```

## 1. Set up the environment

Everything else (MCP servers, tests) runs through [uv](https://docs.astral.sh/uv/),
which resolves Python dependencies from `pyproject.toml` on demand.

```bash
make setup     # installs uv if missing, then `uv sync --all-extras`
```

If `make setup` can't install uv automatically (e.g. no network), install it by
hand — see the uv docs — then re-run `make setup`.

Verify the whole environment (uv, Python deps, Lean, Zulip):

```bash
make check
```

## 2. Run the tests

```bash
make test      # smoke tests: every server imports and constructs
make lint      # ruff over servers/ and skills/
```

## 3. Try the working tools

| Command | What you get |
|---------|--------------|
| `make demo` | Scan the sample Lean project (no deps) |
| `make serve-zulip` | Launch the Zulip search MCP server on stdio |
| `make zulip-status` | Confirm the Zulip server constructs |
| `make check` | Full environment report |

### Zulip search (optional)

To search the Lean community Zulip you need credentials in `~/.zuliprc`:

```ini
[api]
email=YOUR_ZULIP_EMAIL
key=YOUR_API_KEY
site=https://leanprover.zulipchat.com
```

Get an API key at <https://leanprover.zulipchat.com/#settings/account>, then
`chmod 600 ~/.zuliprc`. `make check` will test connectivity.

## 4. Lean (for the proof-checking servers)

The `repl` and `lsp` servers need a working Lean 4 toolchain:

```bash
make lean      # prints how to configure Lean
# elan default stable          # if elan is installed but no toolchain
# or use the /install-lean skill inside a coding assistant
```

> Note: `repl`, `lsp`, and `aristotle` are currently **stubs** — their MCP tools
> return "not implemented". Full reference implementations live in
> [`examples/servers/`](examples/servers/). See
> [CONTRIBUTING.md](CONTRIBUTING.md) to promote one into the live tree.

## 5. Use it inside a coding assistant

Install the plugin (Claude Code shown; see [README.md](README.md) for Codex and
others):

```
/plugin marketplace add /path/to/autoform-bot
/plugin install autoform@autoform
```

Then launch your assistant with the project directory set so the servers and
skills can find it:

```bash
LEAN_PROJECT_DIR=$(pwd) claude
```

Working slash commands today: `/workspace`, `/zulip`, `/install-lean`,
`/setup-project`, `/setup-autoform`.

## Make targets

Run `make help` for the full list.
