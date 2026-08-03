# Zulip configuration

Search the Lean/Mathlib community Zulip ([leanprover.zulipchat.com](https://leanprover.zulipchat.com)) for relevant discussions before formalizing.

## What it does

Searches Zulip for naming conventions, proof strategies, prior art, and API
decisions with the official Python client. Calls are one-shot and do not start a
resident MCP server.

## Usage

Ask Orchestrate naturally: “search Zulip for Hoeffding” or “check if there is a
Zulip discussion about this.”

## API operations

| Client method | Purpose |
|------|---------|
| `search_messages` | Full-text search across messages with optional stream/topic scope |
| `get_messages` | Read a conversation thread by stream and topic |
| `list_streams` | List available streams |
| `get_topics` | List recent topics in a stream |
| `find_zuliprc` | Check which `.zuliprc` is configured |

Invoke these through `servers.zulip.core` as shown in
`internal/runbooks/zulip.md`. That module is a thin wrapper over the official
`zulip` package and returns JSON-serializable dictionaries.

## Setup

Run Setup to check prerequisites and configure access, or set up manually:

### Prerequisites

- **uv** — Python package manager (handles dependencies automatically)
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### Creating a `.zuliprc`

1. Go to [leanprover.zulipchat.com/#settings/account](https://leanprover.zulipchat.com/#settings/account)
2. Scroll to **API key** and click **Get API key**
3. Create the file:

```bash
cat > ~/.zuliprc << 'EOF'
[api]
email=YOUR_ZULIP_EMAIL
key=YOUR_API_KEY
site=https://leanprover.zulipchat.com
EOF
chmod 600 ~/.zuliprc
```

> **Tip:** Use a project-local `.zuliprc` if you work with multiple Zulip
> organizations. Add `.zuliprc` to your `.gitignore`.

### Config file search order

The `.zuliprc` file is searched in order (first found wins):

1. `$ZULIPRC` env var (explicit override)
2. `$LEAN_PROJECT_DIR/.zuliprc` (project-specific)
3. `~/.zuliprc` (standard Zulip client location)
4. `~/.config/.zuliprc`
5. `~/.config/zulip/.zuliprc`
6. `~/.config/zuliprc`

## Dependencies

The `zulip` dependency is managed by the plugin's optional `zulip` extra. Run
the one-shot commands with `uv run --directory <plugin> --extra zulip ...`.
