# Zulip Skill

Search the Lean/Mathlib community Zulip ([leanprover.zulipchat.com](https://leanprover.zulipchat.com)) for relevant discussions before formalizing.

## What it does

Searches Zulip for naming conventions, proof strategies, prior art, and
API decisions. Available as both MCP tools (via the `autoform-zulip` server)
and a standalone CLI script.

## Usage

```
/zulip
```

Or naturally: "search Zulip for Hoeffding", "check if there's a Zulip discussion about this".

## CLI commands

The CLI script wraps `servers/zulip/core.py`:

```bash
python3 skills/zulip/zulip-search.py status                          # check config
python3 skills/zulip/zulip-search.py search "query"                  # search messages
python3 skills/zulip/zulip-search.py search "query" --stream mathlib4  # scoped search
python3 skills/zulip/zulip-search.py streams                         # list streams
python3 skills/zulip/zulip-search.py streams --filter math           # filter streams
python3 skills/zulip/zulip-search.py topics "mathlib4"               # list topics
python3 skills/zulip/zulip-search.py messages "stream" "topic"       # read thread
```

## Setup

Authenticates via a `.zuliprc` file. Searched in order (first found wins):

1. `$ZULIPRC` env var (explicit override)
2. `$LEAN_PROJECT_DIR/.zuliprc` (project-specific)
3. `~/.zuliprc` (standard Zulip client location)
4. `~/.config/.zuliprc`
5. `~/.config/zulip/.zuliprc`
6. `~/.config/zuliprc`

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

## Dependencies

```bash
pip install zulip
```
