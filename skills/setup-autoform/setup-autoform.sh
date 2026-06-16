#!/usr/bin/env bash
# Set up the full autoform environment.
# Checks uv, Python deps, Lean 4, and optional Zulip access.
#
# Usage: bash setup-autoform.sh
# Safe to re-run — skips steps that are already done.

set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"

log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; exit 1; }
skip() { printf '\033[0;37m  - %s\033[0m\n' "$*"; }

# =========================================================================
# 1. uv (required — all MCP servers depend on it)
# =========================================================================
log "Checking uv"

if command -v uv &>/dev/null; then
  ok "uv $(uv --version 2>/dev/null | head -1)"
else
  fail "uv is required but not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# =========================================================================
# 2. Python dependencies (resolve all extras via uv)
# =========================================================================
log "Checking Python dependencies"

all_ok=true

# Core (fastmcp — needed by every MCP server)
if uv run --project "$PLUGIN_ROOT" python -c "import fastmcp; print(f'fastmcp {fastmcp.__version__}')" 2>/dev/null; then
  ok "fastmcp (core)"
else
  log "Installing core dependencies (first run)..."
  if uv run --project "$PLUGIN_ROOT" python -c "import fastmcp; print(f'fastmcp {fastmcp.__version__}')"; then
    ok "fastmcp installed"
  else
    warn "Failed to install fastmcp"; all_ok=false
  fi
fi

# Optional extras
for extra in repl zulip aristotle; do
  pkg="$extra"
  # Map extra name to import name
  case "$extra" in
    repl)      pkg="psutil" ;;
    zulip)     pkg="zulip" ;;
    aristotle) pkg="aristotlelib" ;;
  esac

  if uv run --project "$PLUGIN_ROOT" --extra "$extra" python -c "import $pkg" 2>/dev/null; then
    ok "$extra ($pkg)"
  else
    log "Installing $extra dependencies..."
    if uv run --project "$PLUGIN_ROOT" --extra "$extra" python -c "import $pkg" 2>/dev/null; then
      ok "$extra ($pkg) installed"
    else
      warn "Failed to install $extra extra — $extra server will not work"; all_ok=false
    fi
  fi
done

if [ "$all_ok" = true ]; then
  ok "All Python dependencies available"
fi

# =========================================================================
# 3. Lean 4 (lean + lake)
# =========================================================================
log "Checking Lean 4"

if command -v lean &>/dev/null && command -v lake &>/dev/null; then
  ok "lean $(lean --version 2>/dev/null | head -1)"
  ok "lake $(lake --version 2>/dev/null | head -1)"
else
  if command -v lean &>/dev/null; then
    ok "lean $(lean --version 2>/dev/null | head -1)"
  else
    warn "lean not found"
  fi
  if command -v lake &>/dev/null; then
    ok "lake $(lake --version 2>/dev/null | head -1)"
  else
    warn "lake not found"
  fi
  echo ""
  echo "  Run /install-lean to install Lean 4 and lake."
  echo ""
fi

# =========================================================================
# 4. Zulip credentials (optional)
# =========================================================================
log "Checking Zulip (optional)"

ZULIPRC_FILE=""
for candidate in \
  "${ZULIPRC:-}" \
  "${LEAN_PROJECT_DIR:-.}/.zuliprc" \
  "$HOME/.zuliprc" \
  "$HOME/.config/.zuliprc" \
  "$HOME/.config/zulip/.zuliprc" \
  "$HOME/.config/zuliprc"; do
  if [ -n "$candidate" ] && [ -f "$candidate" ]; then
    ZULIPRC_FILE="$candidate"
    break
  fi
done

if [ -z "$ZULIPRC_FILE" ]; then
  skip "No .zuliprc found — Zulip search will not work"
  echo ""
  echo "  To enable Zulip search:"
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
else
  ok "Found $ZULIPRC_FILE"

  # Check permissions
  perms="$(stat -c '%a' "$ZULIPRC_FILE" 2>/dev/null || /usr/bin/stat -f '%Lp' "$ZULIPRC_FILE" 2>/dev/null || echo "unknown")"
  if [ "$perms" = "600" ]; then
    ok "File permissions: 600"
  elif [ "$perms" != "unknown" ]; then
    warn "File permissions: $perms (recommend 600 — run: chmod 600 $ZULIPRC_FILE)"
  fi

  # Test connectivity
  result="$(uv run --project "$PLUGIN_ROOT" --extra zulip python -c "
import zulip, json
client = zulip.Client(config_file='$ZULIPRC_FILE')
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
    warn "Cannot connect to Zulip: $error — check your .zuliprc credentials"
  fi
fi

# =========================================================================
# Summary
# =========================================================================
echo ""
ok "Autoform setup complete"
