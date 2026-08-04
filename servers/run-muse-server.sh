#!/usr/bin/env bash
set -euo pipefail

server="${1:-}"
if [[ -z "$server" ]]; then
  echo "usage: run-muse-server.sh <lsp|repl>" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin_root="${MUSE_PLUGIN_ROOT:-$(cd "$script_dir/.." && pwd)}"
plugin_data="${MUSE_PLUGIN_DATA_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/autoform-muse}"

mkdir -p "$plugin_data"
cd "$plugin_root"
export PYTHONDONTWRITEBYTECODE=1

case "$server" in
  lsp)
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-lsp"
    exec uv run --project "$plugin_root" --locked python -m servers.lsp.server
    ;;
  repl)
    export UV_PROJECT_ENVIRONMENT="$plugin_data/venv-repl"
    exec uv run --project "$plugin_root" --locked --extra repl python -m servers.repl.server
    ;;
  *)
    echo "unknown Autoform Muse MCP server: $server" >&2
    exit 2
    ;;
esac
