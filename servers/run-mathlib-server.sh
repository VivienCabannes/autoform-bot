#!/usr/bin/env bash
# Launch the Mathlib search MCP server.
# Resolves the Lean project directory BEFORE changing into the plugin root
# (needed for Python imports), so Mathlib is searched in the user's project.

# Resolve the Lean project directory in priority order:
#   1. LEAN_PROJECT_DIR (explicit override)
#   2. CLAUDE_PROJECT_DIR (set by Claude Code to the working project)
#   3. PWD (the directory the server was launched from)
# Skip values that don't resolve to a real directory (e.g. an empty or
# unexpanded "${CLAUDE_PROJECT_DIR}" placeholder).
ORIGINAL_PWD="$PWD"
PROJECT_DIR=""
for candidate in "$LEAN_PROJECT_DIR" "$CLAUDE_PROJECT_DIR" "$ORIGINAL_PWD"; do
  if [ -n "$candidate" ]; then
    resolved="$(cd "$candidate" 2>/dev/null && pwd)"
    if [ -n "$resolved" ]; then
      PROJECT_DIR="$resolved"
      break
    fi
  fi
done
[ -z "$PROJECT_DIR" ] && PROJECT_DIR="$ORIGINAL_PWD"

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$PLUGIN_ROOT"

export LEAN_PROJECT_DIR="$PROJECT_DIR"

# Find a Python interpreter (3.10+) that can import fastmcp.
# LEAN_PLANNER_PYTHON lets the user pin a specific interpreter.
find_python() {
  local candidates=(
    "$LEAN_PLANNER_PYTHON"
    "python3.13" "python3.12" "python3.11" "python3.10"
    "/usr/local/bin/python3" "/opt/homebrew/bin/python3"
    "python3" "python"
  )
  for py in "${candidates[@]}"; do
    [ -z "$py" ] && continue
    command -v "$py" >/dev/null 2>&1 || continue
    if "$py" -c "import sys, fastmcp; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1; then
      echo "$py"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python)"
if [ -z "$PYTHON" ]; then
  echo "Error: no Python 3.10+ with fastmcp found. Install with: pip install fastmcp" >&2
  exit 1
fi

exec "$PYTHON" -m servers.mathlib.server
