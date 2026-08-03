#!/usr/bin/env bash
set -euo pipefail

server="${1:-}"
if [[ -z "$server" ]]; then
  echo "usage: run-muse-server.sh <lean-lsp|prover>" >&2
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
  lean-lsp)
    cd "$workspace"
    exec lean-lsp-mcp --disable-tools lean_leansearch,lean_loogle,lean_leanfinder
    ;;
  prover)
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-prover"
    exec uv run python -m servers.prover.server
    ;;
  *)
    echo "unknown Autoform Muse MCP server: $server" >&2
    exit 2
    ;;
esac
