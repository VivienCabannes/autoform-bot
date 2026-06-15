#!/usr/bin/env bash
# Set up Zulip access for the autoform plugin.
# Checks uv, verifies the zulip package, validates .zuliprc, tests connectivity.
#
# Usage: bash setup-zulip.sh
# Safe to re-run — skips steps that are already done.

set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"

log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; exit 1; }

# --- Check uv ---
log "Checking prerequisites"

if command -v uv &>/dev/null; then
  ok "uv $(uv --version 2>/dev/null | head -1)"
else
  fail "uv is required but not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# --- Check zulip package via uv ---
log "Checking Python dependencies"

if uv run --project "$PLUGIN_ROOT" --extra zulip python -c "import zulip; print(f'zulip {zulip.__version__}')" 2>/dev/null; then
  ok "zulip package available"
else
  log "Installing zulip package (first run)..."
  if uv run --project "$PLUGIN_ROOT" --extra zulip python -c "import zulip; print(f'zulip {zulip.__version__}')"; then
    ok "zulip package installed"
  else
    fail "Failed to install zulip package. Check pyproject.toml in $PLUGIN_ROOT"
  fi
fi

# --- Check .zuliprc ---
log "Checking Zulip credentials"

ZULIPRC=""
for candidate in \
  "${ZULIPRC:-}" \
  "${LEAN_PROJECT_DIR:-.}/.zuliprc" \
  "$HOME/.zuliprc" \
  "$HOME/.config/.zuliprc" \
  "$HOME/.config/zulip/.zuliprc" \
  "$HOME/.config/zuliprc"; do
  if [ -n "$candidate" ] && [ -f "$candidate" ]; then
    ZULIPRC="$candidate"
    break
  fi
done

if [ -z "$ZULIPRC" ]; then
  warn "No .zuliprc file found"
  echo ""
  echo "  To create one:"
  echo ""
  echo "  1. Go to https://leanprover.zulipchat.com/#settings/account"
  echo "  2. Scroll to 'API key' and click 'Get API key'"
  echo "  3. Run:"
  echo ""
  echo "     cat > ~/.zuliprc << 'EOF'"
  echo "     [api]"
  echo "     email=YOUR_ZULIP_EMAIL"
  echo "     key=YOUR_API_KEY"
  echo "     site=https://leanprover.zulipchat.com"
  echo "     EOF"
  echo "     chmod 600 ~/.zuliprc"
  echo ""
  echo "  Then re-run /setup-zulip to verify."
  exit 1
fi

ok "Found $ZULIPRC"

# Check permissions (should be 600)
perms="$(stat -c '%a' "$ZULIPRC" 2>/dev/null || /usr/bin/stat -f '%Lp' "$ZULIPRC" 2>/dev/null || echo "unknown")"
if [ "$perms" = "600" ]; then
  ok "File permissions: 600 (good)"
elif [ "$perms" != "unknown" ]; then
  warn "File permissions: $perms (recommend 600 — run: chmod 600 $ZULIPRC)"
fi

# --- Test connectivity ---
log "Testing Zulip connectivity"

result="$(uv run --project "$PLUGIN_ROOT" --extra zulip python -c "
import zulip, json, sys
client = zulip.Client(config_file='$ZULIPRC')
r = client.get_server_settings()
if r.get('result') == 'success':
    print(json.dumps({'ok': True, 'version': r.get('zulip_version', '?')}))
else:
    print(json.dumps({'ok': False, 'error': r.get('msg', 'unknown')}))
" 2>/dev/null)" || result='{"ok": false, "error": "Failed to run connectivity test"}'

is_ok="$(printf '%s' "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok', False))" 2>/dev/null || echo "False")"

if [ "$is_ok" = "True" ]; then
  version="$(printf '%s' "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version', '?'))" 2>/dev/null || echo "?")"
  ok "Connected to Zulip (server version $version)"
else
  error="$(printf '%s' "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error', 'unknown'))" 2>/dev/null || echo "unknown")"
  fail "Cannot connect to Zulip: $error — check your .zuliprc credentials"
fi

echo ""
ok "Zulip is ready — use /zulip to search community discussions"
