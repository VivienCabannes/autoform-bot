# Zulip Skill

Search the Lean/Mathlib community Zulip ([leanprover.zulipchat.com](https://leanprover.zulipchat.com)) for relevant discussions before formalizing.

## What it does

Searches Zulip for naming conventions, proof strategies, prior art, and
API decisions via the `autoform-zulip` MCP server.

## Usage

```
/zulip
```

Or naturally: "search Zulip for Hoeffding", "check if there's a Zulip discussion about this".

## MCP tools

| Tool | Purpose |
|------|---------|
| `zulip_search` | Full-text search across messages (with optional stream/topic scope) |
| `zulip_messages` | Read a conversation thread by stream + topic |
| `zulip_streams` | List available streams (filterable) |
| `zulip_topics` | List recent topics in a stream |
| `zulip_status` | Check if `.zuliprc` is configured |

## Setup

Run `/setup-autoform` to check prerequisites and configure access, or set up manually:

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

Python dependencies (`zulip`, `fastmcp`) are managed automatically by `uv`
from the plugin's `pyproject.toml` — no manual `pip install` needed.
