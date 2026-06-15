# Zulip Skill

Search the Lean/Mathlib community Zulip ([leanprover.zulipchat.com](https://leanprover.zulipchat.com)) for relevant discussions before formalizing.

## What it does

A self-contained Python script that searches Zulip for naming conventions,
proof strategies, prior art, and API decisions. No MCP server — just run
the script via bash and read the JSON output.

## Usage

```
/zulip
```

Or naturally: "search Zulip for Hoeffding", "check if there's a Zulip discussion about this".

## Commands

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

The script authenticates via a `.zuliprc` file. It searches these locations
in order (first found wins):

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

Replace `YOUR_ZULIP_EMAIL` and `YOUR_API_KEY` with your actual credentials.

> **Tip:** Use a project-local `.zuliprc` (at the root of your git repo) if you
> work with multiple Zulip organizations. Add `.zuliprc` to your `.gitignore`
> to avoid leaking credentials.

## Dependencies

```bash
pip install zulip
```
