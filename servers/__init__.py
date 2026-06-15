"""Autoform MCP servers — standalone Lean 4 tooling.

Each server is independent and can be started separately:
    python -m servers.repl       # Lean REPL pool
    python -m servers.lsp        # Lean LSP diagnostics
    python -m servers.aristotle  # Aristotle (Harmonic) prover
    python -m servers.zulip      # Zulip community search
"""
