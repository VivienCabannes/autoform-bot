#!/usr/bin/env bash
# check_toolchain.sh — verify the Lean Informal Planner blueprint-view toolchain.
#
# Checks, with clear PASS/FAIL and the exact fix command for each failure:
#   - a Python >= 3.10 that can import the blueprint toolchain
#   - graphviz `dot` on PATH
#   - python imports: plasTeX, plastexdepgraph, plastexshowmore, leanblueprint, pygraphviz
#
# The web (HTML) blueprint build needs NO LaTeX — it is pure-Python plasTeX.
#
# Exits 0 only if every check passes.
#
# Environment overrides:
#   LEAN_PLANNER_PYTHON   pin a specific Python interpreter to use/check first.

set -uo pipefail

# --- pretty output (degrade gracefully if not a TTY) -------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

FAILURES=0

pass() { printf "%s[PASS]%s %s\n" "$GREEN" "$RESET" "$1"; }
fail() {
  printf "%s[FAIL]%s %s\n" "$RED" "$RESET" "$1"
  if [ -n "${2:-}" ]; then
    printf "       %sfix:%s %s\n" "$YELLOW" "$RESET" "$2"
  fi
  FAILURES=$((FAILURES + 1))
}

# The python packages we need, as "import name|pip name" pairs.
# (Most match, but the import vs pip-distribution names are listed explicitly.)
PKG_SPECS=(
  "plasTeX|plasTeX"
  "plastexdepgraph|plastexdepgraph"
  "plastexshowmore|plastexshowmore"
  "leanblueprint|leanblueprint"
  "pygraphviz|pygraphviz"
)

# --- locate a suitable Python ------------------------------------------------
# Search candidate interpreters the same way run-mathlib-server.sh does, but
# require >= 3.10 (we don't require any import here — we want to find the *best*
# interpreter, then report on what it's missing).
find_python() {
  local candidates=(
    "${LEAN_PLANNER_PYTHON:-}"
    "python3.13" "python3.12" "python3.11" "python3.10"
    "/usr/local/bin/python3" "/opt/homebrew/bin/python3"
    "python3" "python"
  )
  # A pinned interpreter is authoritative: if LEAN_PLANNER_PYTHON is set and is
  # a valid Python >= 3.10, use exactly it so the report reflects that one
  # (don't silently fall back to a different interpreter that happens to work).
  local pin="${LEAN_PLANNER_PYTHON:-}"
  if [ -n "$pin" ] && command -v "$pin" >/dev/null 2>&1 \
     && "$pin" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1; then
    echo "$pin"
    return 0
  fi
  # First pass: prefer an interpreter that can already import everything.
  local py
  for py in "${candidates[@]}"; do
    [ -z "$py" ] && continue
    command -v "$py" >/dev/null 2>&1 || continue
    "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1 || continue
    if "$py" -c "import plasTeX, plastexdepgraph, plastexshowmore, leanblueprint, pygraphviz" >/dev/null 2>&1; then
      echo "$py"
      return 0
    fi
  done
  # Second pass: any >= 3.10 interpreter (so we can report what's missing on it).
  for py in "${candidates[@]}"; do
    [ -z "$py" ] && continue
    command -v "$py" >/dev/null 2>&1 || continue
    if "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1; then
      echo "$py"
      return 0
    fi
  done
  return 1
}

# pip invocation string for the chosen interpreter (for fix hints).
pip_for() {
  printf '%s -m pip' "$1"
}

printf "%s== Lean Informal Planner toolchain check ==%s\n\n" "$BOLD" "$RESET"

# --- 1. Python >= 3.10 -------------------------------------------------------
PYTHON="$(find_python)"
if [ -z "$PYTHON" ]; then
  fail "Python >= 3.10 not found" \
       "install Python 3.10+ (e.g. 'brew install python@3.12'), or set LEAN_PLANNER_PYTHON=/path/to/python3"
  # Without a Python we cannot run any of the import checks below.
  printf "\n%sSummary:%s %s%d check(s) failed%s — see fixes above.\n" \
    "$BOLD" "$RESET" "$RED" "$FAILURES" "$RESET"
  printf "After installing, re-run this script and consult SETUP.md.\n"
  exit 1
fi

PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
PY_REAL="$(command -v "$PYTHON" 2>/dev/null || echo "$PYTHON")"
pass "Python >= 3.10  (using $PY_REAL, version $PY_VERSION)"

PIP="$(pip_for "$PYTHON")"

# --- 2. graphviz `dot` on PATH ----------------------------------------------
if command -v dot >/dev/null 2>&1; then
  DOT_VERSION="$(dot -V 2>&1 | head -n1)"
  pass "graphviz 'dot' on PATH  ($DOT_VERSION)"
else
  if [[ "$OSTYPE" == darwin* ]] || command -v brew >/dev/null 2>&1; then
    fail "graphviz 'dot' not on PATH" "brew install graphviz"
  else
    fail "graphviz 'dot' not on PATH" \
         "install graphviz (e.g. 'sudo apt-get install graphviz' or 'brew install graphviz')"
  fi
fi

# --- 3. python imports -------------------------------------------------------
# pygraphviz needs graphviz dev headers at build time; provide the documented
# fix recipe so a failed import gets an actionable command.
pygraphviz_fix() {
  if command -v brew >/dev/null 2>&1; then
    cat <<EOF
brew install graphviz && \\
CFLAGS="-I\$(brew --prefix graphviz)/include" \\
LDFLAGS="-L\$(brew --prefix graphviz)/lib" \\
  $PIP install pygraphviz
       (on nonstandard/system Python add '-undefined dynamic_lookup' to LDFLAGS; a venv from a standard Python avoids it — see SETUP.md)
EOF
  else
    printf '%s\n' "install graphviz dev headers (e.g. 'sudo apt-get install graphviz graphviz-dev'), then: $PIP install pygraphviz"
  fi
}

for spec in "${PKG_SPECS[@]}"; do
  import_name="${spec%%|*}"
  pip_name="${spec##*|}"
  if "$PYTHON" -c "import ${import_name}" >/dev/null 2>&1; then
    pass "python import: ${import_name}"
  else
    if [ "$import_name" = "pygraphviz" ]; then
      # Distinguish "not installed" from "installed but its graphviz runtime
      # libs aren't on the loader path" — the latter is expected on platform
      # Pythons and is handled by the Makefile (setup-gvlibs + LD_LIBRARY_PATH),
      # so the right fix is NOT 'pip install'.
      pgv_err="$("$PYTHON" -c "import pygraphviz" 2>&1)"
      if printf '%s' "$pgv_err" | grep -qiE "cannot open shared object|lib(cdt|gvc|cgraph|pathplan|xdot)\.so|\.so(\.[0-9]+)*: cannot"; then
        fail "python import: pygraphviz  (installed, but its graphviz runtime libs are not on the loader path)" \
             "expected on platform Pythons — 'make setup-venv' curates the libs into .lean-deps/gvlibs/ and 'make web' loads them via LD_LIBRARY_PATH. To verify now: LD_LIBRARY_PATH=.lean-deps/gvlibs $PYTHON -c 'import pygraphviz'"
      else
        fail "python import: pygraphviz  (cannot 'import pygraphviz')" "$(pygraphviz_fix)"
      fi
    else
      fail "python import: ${import_name}  (cannot 'import ${import_name}')" \
           "$PIP install ${pip_name}"
    fi
  fi
done

# --- summary -----------------------------------------------------------------
printf "\n"
if [ "$FAILURES" -eq 0 ]; then
  printf "%sSummary:%s %sall checks passed%s — toolchain ready.\n" \
    "$BOLD" "$RESET" "$GREEN" "$RESET"
  printf "Note: serve the built view over a local HTTP server (e.g. 'leanblueprint serve'),\n"
  printf "      NOT file:// — the WASM dep-graph workers won't load from file://.\n"
  exit 0
else
  printf "%sSummary:%s %s%d check(s) failed%s — apply the fixes above.\n" \
    "$BOLD" "$RESET" "$RED" "$FAILURES" "$RESET"
  printf "See SETUP.md for the full recipe and the recommended venv path.\n"
  exit 1
fi
