#!/usr/bin/env bash
# Set up the full autoform environment.
# Checks uv, Python dependencies, and Lean 4.
#
# Usage: bash install-autoform.sh
# Safe to re-run — skips steps that are already done.

set -euo pipefail

AUTOFORM_RESOLVED_ROOT="$(
  if [ -n "${AUTOFORM_PLUGIN_ROOT:-}" ]; then
    printf '%s' "$AUTOFORM_PLUGIN_ROOT"
  elif [ -n "${PLUGIN_ROOT:-}" ]; then
    printf '%s' "$PLUGIN_ROOT"
  elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    printf '%s' "$CLAUDE_PLUGIN_ROOT"
  else
    cd "$(dirname "$0")/.." && pwd
  fi
)"

log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; exit 1; }
skip() { printf '\033[0;37m  - %s\033[0m\n' "$*"; }

# =========================================================================
# 1. uv (required — both MCP servers depend on it)
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

# Core (fastmcp — needed by both MCP servers)
if uv run --project "$AUTOFORM_RESOLVED_ROOT" python -c "import fastmcp; print(f'fastmcp {fastmcp.__version__}')" 2>/dev/null; then
  ok "fastmcp (core)"
else
  log "Installing core dependencies (first run)..."
  if uv run --project "$AUTOFORM_RESOLVED_ROOT" python -c "import fastmcp; print(f'fastmcp {fastmcp.__version__}')"; then
    ok "fastmcp installed"
  else
    warn "Failed to install fastmcp"; all_ok=false
  fi
fi

# Optional extras
for extra in repl aristotle; do
  pkg="$extra"
  # Map extra name to import name
  case "$extra" in
    repl)      pkg="psutil" ;;
    aristotle) pkg="aristotlelib" ;;
  esac

  if uv run --project "$AUTOFORM_RESOLVED_ROOT" --extra "$extra" python -c "import $pkg" 2>/dev/null; then
    ok "$extra ($pkg)"
  else
    log "Installing $extra dependencies..."
    if uv run --project "$AUTOFORM_RESOLVED_ROOT" --extra "$extra" python -c "import $pkg" 2>/dev/null; then
      ok "$extra ($pkg) installed"
    else
      warn "Failed to install $extra extra — $extra integration will not work"; all_ok=false
    fi
  fi
done

if [ "$all_ok" = true ]; then
  ok "All Python dependencies available"
fi

# =========================================================================
# 3. PDF tools (poppler-utils — pdftotext/pdftoppm, needed to read source PDFs)
# =========================================================================
log "Checking PDF tools (poppler-utils)"

if command -v pdftotext &>/dev/null && command -v pdftoppm &>/dev/null; then
  ok "poppler-utils ($(pdftotext -v 2>&1 | head -1))"
else
  log "Installing poppler-utils..."
  if command -v brew &>/dev/null; then
    brew install poppler || warn "brew install poppler failed"
  elif command -v apt-get &>/dev/null; then
    sudo apt-get install -y poppler-utils || warn "apt-get install poppler-utils failed"
  elif command -v dnf &>/dev/null; then
    sudo dnf install -y poppler-utils || warn "dnf install poppler-utils failed"
  else
    warn "No supported package manager found; install poppler-utils manually (provides pdftotext/pdftoppm)"
  fi
  if command -v pdftotext &>/dev/null && command -v pdftoppm &>/dev/null; then
    ok "poppler-utils installed"
  else
    warn "poppler-utils still missing — reading source PDFs (pdftotext / Read of a PDF) will not work"
  fi
fi

# =========================================================================
# 4. Lean 4 (lean + lake)
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
  echo "  Run Autoform Setup to install Lean 4 and lake."
  echo ""
fi

# =========================================================================
# 5. Lean Explore API key (optional)
# =========================================================================
log "Checking Lean Explore (optional)"

if [ -n "${LEANEXPLORE_API_KEY:-}" ]; then
  ok "LEANEXPLORE_API_KEY set — semantic Mathlib search integration is available"
else
  skip "LEANEXPLORE_API_KEY not set — semantic Mathlib search is optional"
  echo ""
  echo "  To enable semantic Mathlib search:"
  echo "    1. Get a key at https://www.leanexplore.com"
  echo "    2. export LEANEXPLORE_API_KEY=<your-key>"
  echo ""
fi

# =========================================================================
# Summary
# =========================================================================
echo ""
ok "Autoform setup complete"
