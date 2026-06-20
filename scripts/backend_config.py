#!/usr/bin/env python3
"""Persistent prover-backend selection — shared by ``/autoform:set-backend`` (writes),
``/autoform:dispatch`` (reads), and the DAG review dashboard's backend dropdown
(reads/writes the same file). Deterministic, zero model tokens.

The chosen backend is the *swappable parameter* of the unified prover MCP
(``servers/prover``, added by the prover PR): the orchestrator (the Claude Code
session) stays the brain; only the backend that *proves a node* changes. **Backend is
also the billing path** — ``max`` runs on the Max subscription, ``aristotle`` on
Harmonic's key, ``codex`` (future) on its own auth.

Each user-facing backend maps to the ``prove_node(node, backend=...)`` adapter id via
its ``prover`` field (``max -> "claude"``, ``aristotle -> "aristotle"``), so the
dispatch command never hard-codes the mapping.

Config: a small JSON at ``~/.autoform/config.json`` (override with ``$AUTOFORM_CONFIG``)::

    {"backend": "max"}

Usage::

  backend_config.py get               # current user-facing backend (default: max)
  backend_config.py prover [<id>]     # the prove_node adapter id for <id> (or current)
  backend_config.py set <backend>     # validate + persist
  backend_config.py list              # known backends (* = current) + billing
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Known backends. ``available`` = an adapter exists in servers/prover today; ``prover``
# = the id passed to the prove_node MCP tool. ``codex`` is listed for forward-compat
# (selecting it warns until its adapter lands).
BACKENDS: dict[str, dict] = {
    "max": {"label": "Claude Max", "available": True, "prover": "claude",
            "billing": "Max subscription · no API tokens"},
    "aristotle": {"label": "Aristotle", "available": True, "prover": "aristotle",
                  "billing": "Harmonic · ARISTOTLE_API_KEY"},
    "codex": {"label": "Codex", "available": False, "prover": "codex",
              "billing": "Codex · its own auth (planned)"},
}
DEFAULT_BACKEND = "max"


def _config_path() -> Path:
    return Path(os.environ.get("AUTOFORM_CONFIG",
                               str(Path.home() / ".autoform" / "config.json")))


def get_backend() -> str:
    """The persisted user-facing backend, or ``max`` if unset/unreadable/unknown."""
    try:
        data = json.loads(_config_path().read_text())
        if data.get("backend") in BACKENDS:
            return data["backend"]
    except Exception:
        pass
    return DEFAULT_BACKEND


def prover_of(backend: str) -> str:
    """The prove_node adapter id for a user-facing backend (e.g. max -> claude)."""
    return BACKENDS.get(backend, {}).get("prover", "claude")


def set_backend(backend: str) -> str:
    """Validate + persist ``backend`` (atomic write). Raises SystemExit on unknown."""
    if backend not in BACKENDS:
        raise SystemExit(f"unknown backend {backend!r}; known: {', '.join(BACKENDS)}")
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data["backend"] = backend
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return backend


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Get/set the autoform prover backend.")
    ap.add_argument("cmd", choices=["get", "prover", "set", "list"])
    ap.add_argument("backend", nargs="?")
    a = ap.parse_args(argv)

    if a.cmd == "get":
        print(get_backend())
        return 0
    if a.cmd == "prover":
        print(prover_of(a.backend or get_backend()))
        return 0
    if a.cmd == "list":
        cur = get_backend()
        for name, m in BACKENDS.items():
            mark = "*" if name == cur else " "
            planned = "" if m["available"] else "  (planned — adapter not yet implemented)"
            print(f" {mark} {name:10} → prove_node backend={m['prover']:10} — {m['billing']}{planned}")
        return 0
    # set
    if not a.backend:
        ap.error("set needs a backend (max | aristotle | codex)")
    b = set_backend(a.backend)
    m = BACKENDS[b]
    warn = "" if m["available"] else "  ⚠ adapter not yet implemented — dispatch will error until it lands"
    print(f"backend set to '{b}' (prove_node backend={m['prover']}) — billing: {m['billing']}{warn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
