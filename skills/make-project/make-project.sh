#!/usr/bin/env bash
# Set up a new Lean 4 + Mathlib project from the LeanProject template.
#
# Usage: bash scripts/make-project.sh <ProjectName> [target-dir]
#
# - Clones the LeanProject template
# - Renames everything to <ProjectName>
# - Fetches Mathlib cache (~2 GB)
# - Runs lake build to verify
#
# Requires: git, python3, lean/lake (install via scripts/install-lean.sh)

set -euo pipefail

log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; exit 1; }

# --- Args ---
if [ $# -lt 1 ]; then
  echo "Usage: make-project.sh <ProjectName> [target-dir]"
  echo ""
  echo "  ProjectName   UpperCamelCase name (e.g. ConvexBodies, PrimeGaps)"
  echo "  target-dir    Where to create the project (default: ./<ProjectName>)"
  exit 1
fi

PROJECT_NAME="$1"
TARGET_DIR="${2:-./$PROJECT_NAME}"

# --- Prereqs ---
log "Checking prerequisites"
for cmd in git python3 lake; do
  if ! command -v "$cmd" &>/dev/null; then
    fail "$cmd not found. Run /install-lean first."
  fi
  ok "$cmd"
done

# --- Clone ---
if [ -d "$TARGET_DIR" ]; then
  fail "Directory $TARGET_DIR already exists"
fi

log "Cloning LeanProject template into $TARGET_DIR"
git clone https://github.com/leanprover-community/LeanProject.git "$TARGET_DIR"
cd "$TARGET_DIR"
rm -rf .git
git init
ok "Template cloned"

# --- Rename ---
log "Renaming project to $PROJECT_NAME"
python3 scripts/customize_template.py "$PROJECT_NAME"
ok "Project renamed"

# --- Mathlib cache ---
log "Fetching Mathlib cache (this may take a few minutes)"
lake exe cache get
ok "Mathlib cache fetched"

# --- Build ---
log "Building project"
if lake build; then
  ok "Build succeeded"
else
  fail "Build failed — check errors above"
fi

# --- Done ---
echo ""
ok "Project $PROJECT_NAME is ready at $TARGET_DIR"
echo ""
echo "  cd $TARGET_DIR"
echo "  # Edit $PROJECT_NAME/Example.lean to get started"
echo ""
echo "Next: use /autoform-extract to identify formalization targets from your source material."
