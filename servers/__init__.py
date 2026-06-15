"""Autoform MCP servers — standalone Lean 4 tooling.

Each server is independent and can be started separately via uv:
    uv run python -m servers.repl                    # Lean REPL pool
    uv run python -m servers.lsp                     # Lean LSP diagnostics
    uv run --extra aristotle python -m servers.aristotle  # Aristotle prover
    uv run --extra zulip python -m servers.zulip     # Zulip community search
"""
