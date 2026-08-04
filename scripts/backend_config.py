#!/usr/bin/env python3
"""Persistent prover-backend selection — shared by ``/autoform:set-backend`` (writes),
``/autoform:orchestrate`` (reads), and the DAG review dashboard's backend dropdown
(reads/writes the same file). Deterministic, zero model tokens.

The chosen backend is the *swappable parameter* of the unified prover runtime
(``autoform/prover``): the orchestrator stays the brain; only the backend that
*proves a node* changes. **Backend is
also the billing path** — ``max`` runs on the Max subscription, ``aristotle`` on
Harmonic's key, ``codex`` on its own auth, or an explicitly configured
OpenAI-compatible endpoint.

Each user-facing backend maps to the dispatcher adapter id via
its ``prover`` field (``max -> "claude"``, ``aristotle -> "aristotle"``), so the
dispatch command never hard-codes the mapping.

Config: a small JSON at ``~/.autoform/config.json`` (override with ``$AUTOFORM_CONFIG``)::

    {"backend": "max"}

Usage::

  backend_config.py get               # current user-facing backend (default: max)
  backend_config.py get --fallback codex  # host-native default when no choice exists
  backend_config.py prover [<id>]     # the prove_node adapter id for <id> (or current)
  backend_config.py set <backend>     # validate + persist
  backend_config.py list              # known backends (* = current) + billing
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Known backends. ``available`` means an adapter exists in autoform/prover;
# ``prover`` is the id passed to the dispatcher runtime.
BACKENDS: dict[str, dict] = {
    "max": {"label": "Claude Code (Max subscription)", "available": True,
            "prover": "claude",
            "billing": "Claude Max subscription · API credentials disabled"},
    "aristotle": {"label": "Aristotle", "available": True, "prover": "aristotle",
                  "billing": "Harmonic · ARISTOTLE_API_KEY"},
    "codex": {"label": "Codex", "available": True, "prover": "codex",
              "billing": "Codex · its own auth (ChatGPT/OpenAI login)"},
    "openai": {"label": "Custom API (OpenAI-compatible)", "available": True,
               "prover": "openai",
               "billing": "Configured API credential · project data may leave the machine"},
    "avocado": {"label": "Meta Avocado", "available": True, "prover": "avocado",
                "billing": "Meta API/gateway · configured credential (project data may leave the machine)"},
}
DEFAULT_BACKEND = "max"


def _config_path() -> Path:
    return Path(os.environ.get("AUTOFORM_CONFIG",
                               str(Path.home() / ".autoform" / "config.json")))


def get_backend(fallback: str = DEFAULT_BACKEND) -> str:
    """Return the persisted backend, or the caller's validated fallback.

    Interactive hosts pass their native backend as ``fallback``.  This changes
    only the no-config/error case; an explicit persisted user choice always wins.
    """
    if fallback not in BACKENDS:
        raise ValueError(
            f"unknown fallback {fallback!r}; known: {', '.join(BACKENDS)}"
        )
    path = _config_path()
    if not path.exists():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read valid backend config at {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"backend config at {path} must be a JSON object")
    selected = data.get("backend")
    if selected is None:
        return fallback
    if selected not in BACKENDS:
        raise ValueError(
            f"unknown persisted backend {selected!r} in {path}; "
            f"known: {', '.join(BACKENDS)}"
        )
    return str(selected)


def prover_of(backend: str) -> str:
    """The prove_node adapter id for a user-facing backend (e.g. max -> claude)."""
    try:
        return str(BACKENDS[backend]["prover"])
    except KeyError as error:
        raise ValueError(
            f"unknown backend {backend!r}; known: {', '.join(BACKENDS)}"
        ) from error


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
    ap.add_argument(
        "--fallback",
        choices=BACKENDS,
        default=DEFAULT_BACKEND,
        help="default when no valid persisted choice exists (default: max)",
    )
    a = ap.parse_args(argv)

    try:
        if a.cmd == "get":
            print(get_backend(a.fallback))
            return 0
        if a.cmd == "prover":
            print(prover_of(a.backend or get_backend(a.fallback)))
            return 0
        if a.cmd == "list":
            cur = get_backend(a.fallback)
            for name, m in BACKENDS.items():
                mark = "*" if name == cur else " "
                planned = "" if m["available"] else "  (planned — adapter not yet implemented)"
                print(
                    f" {mark} {name:10} {m['label']} "
                    f"→ prove_node backend={m['prover']:10} — {m['billing']}{planned}"
                )
            return 0
    except ValueError as error:
        ap.error(str(error))
        return 2
    # set
    if not a.backend:
        ap.error(f"set needs a backend ({' | '.join(BACKENDS)})")
    b = set_backend(a.backend)
    m = BACKENDS[b]
    warn = "" if m["available"] else "  ⚠ adapter not yet implemented — dispatch will error until it lands"
    print(
        f"backend set to '{b}' — {m['label']} "
        f"(prove_node backend={m['prover']}) — billing: {m['billing']}{warn}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
