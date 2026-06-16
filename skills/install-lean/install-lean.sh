#!/usr/bin/env bash
# Install Lean 4 via elan (the Lean version manager).
# Checks if lean/lake are already available, installs elan if not, verifies.
#
# Usage: bash install-lean.sh
# Safe to re-run — skips steps that are already done.

set -euo pipefail

log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; exit 1; }

# --- Already installed? ---
if command -v lean &>/dev/null && command -v lake &>/dev/null; then
  ok "Lean already installed ($(lean --version 2>/dev/null | head -1))"
  ok "Lake already installed ($(lake --version 2>/dev/null | head -1))"
  exit 0
fi

# --- Prerequisites ---
log "Checking prerequisites"

for cmd in git curl; do
  if ! command -v "$cmd" &>/dev/null; then
    fail "$cmd is required but not found. Install it first."
  fi
  ok "$cmd"
done

# --- elan ---
if command -v elan &>/dev/null; then
  ok "elan already installed ($(elan --version 2>/dev/null | head -1))"
else
  log "Installing elan"
  curl https://elan.lean-lang.org/elan-init.sh -sSf | sh -s -- -y --default-toolchain none
  export PATH="$HOME/.elan/bin:$PATH"

  if ! command -v elan &>/dev/null; then
    fail "elan install failed — ~/.elan/bin not on PATH"
  fi
  ok "elan installed"
fi

# --- Lean toolchain ---
if command -v lean &>/dev/null; then
  ok "lean already available ($(lean --version 2>/dev/null | head -1))"
else
  log "Installing default Lean toolchain"
  elan toolchain install stable
  elan default stable
  ok "lean installed ($(lean --version 2>/dev/null | head -1))"
fi

# --- Verify ---
log "Verifying"
lean --version  || fail "lean not working"
lake --version  || fail "lake not working"

ok "Lean 4 is ready"
