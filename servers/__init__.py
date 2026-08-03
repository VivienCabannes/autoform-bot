"""Autoform's stateful prover servers and shared implementation code.

The plugin uses the external ``lean-lsp-mcp`` executable for stateful Lean
goals and diagnostics. Mathlib and Zulip helpers in this package are imported
by one-shot CLI/API calls; they are not registered as MCP servers.
"""
