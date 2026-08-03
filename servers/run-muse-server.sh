#!/usr/bin/env bash
set -euo pipefail

server="${1:-}"
if [[ -z "$server" ]]; then
  echo "usage: run-muse-server.sh <mathlib|repl|lsp|aristotle|prover|zulip>" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin_root="${MUSE_PLUGIN_ROOT:-$(cd "$script_dir/.." && pwd)}"
plugin_data="${MUSE_PLUGIN_DATA_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/autoform-muse}"
workspace="${LEAN_PROJECT_DIR:-$PWD}"

mkdir -p "$plugin_data"
cd "$plugin_root"
export PYTHONDONTWRITEBYTECODE=1

case "$server" in
  mathlib)
    export LEAN_PROJECT_DIR="$workspace"
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-core"
    exec uv run python -m servers.mathlib.server
    ;;
  repl)
    export LEAN_PROJECT_DIR="$workspace"
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-repl"
    exec uv run --extra repl python -m servers.repl.server
    ;;
  lsp)
    export LEAN_PROJECT_DIR="$workspace"
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-core"
    exec uv run python -m servers.lsp.server
    ;;
  aristotle)
    export ARISTOTLE_DOWNLOAD_DIR="${ARISTOTLE_DOWNLOAD_DIR:-$plugin_data/aristotle-output}"
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-aristotle"
    exec uv run --extra aristotle python -m servers.aristotle.server
    ;;
  prover)
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-core"
    exec uv run python -m servers.prover.server
    ;;
  zulip)
    export LEAN_PROJECT_DIR="$workspace"
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-zulip"
    exec uv run --extra zulip python -m servers.zulip.server
    ;;
  *)
    echo "unknown Autoform Muse MCP server: $server" >&2
    exit 2
    ;;
esac
