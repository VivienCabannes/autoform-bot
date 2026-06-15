# Zulip Skill

Search the Lean/Mathlib community Zulip ([leanprover.zulipchat.com](https://leanprover.zulipchat.com)) for relevant discussions before formalizing.

## What it does

Teaches the agent to search Zulip for naming conventions, proof strategies,
prior art, and API decisions before writing Lean code. Tells the agent when
and how to search, with key streams to check.

## Usage

```
/zulip
```

Or naturally: "search Zulip for Hoeffding", "check if there's a Zulip discussion about this".

## Available tools

| Tool | Purpose |
|------|---------|
| `zulip_search` | Full-text search across messages (with optional stream/topic scope) |
| `zulip_messages` | Read a conversation thread by stream + topic |
| `zulip_streams` | List available streams (filterable) |
| `zulip_topics` | List recent topics in a stream |
| `zulip_status` | Check if `.zuliprc` is configured |

## Setup

The Zulip MCP server authenticates via a `.zuliprc` file. The plugin searches
these locations in order (first found wins):

1. `$ZULIPRC` env var (explicit override)
2. `$LEAN_PROJECT_DIR/.zuliprc` (project-specific)
3. `~/.zuliprc` (standard Zulip client location)
4. `~/.config/zulip/.zuliprc`
5. `~/.config/zuliprc`

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

Replace `YOUR_ZULIP_EMAIL` and `YOUR_API_KEY` with your actual credentials.

> **Tip:** Use a project-local `.zuliprc` (at the root of your git repo) if you
> work with multiple Zulip organizations. Add `.zuliprc` to your `.gitignore`
> to avoid leaking credentials.

### Verifying setup

Run `zulip_status` — it will report which config file was found and which site
it points to, without revealing the API key.

## Dependencies

The `zulip` Python package is required:

```bash
pip install zulip
# or, from the autoform plugin:
pip install 'autoform[zulip]'
```
