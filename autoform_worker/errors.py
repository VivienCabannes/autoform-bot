"""Worker exit discipline — the three ways a round ends.

``Die`` is an operator-facing error (exit 1). ``NoProgress`` is the honest
"nothing actionable" signal (exit 75, the loop backs off instead of retrying
hot). Everything else is a bug and propagates.
"""
from __future__ import annotations

EX_OK = 0
EX_ERROR = 1
EX_NOPROGRESS = 75
EX_INT = 130
EX_TERM = 143


class Die(RuntimeError):
    """Fatal, operator-facing error — print the message, exit 1."""


class NoProgress(RuntimeError):
    """The round found nothing actionable (or could not leave a visible mark).

    Exit 75 — distinct from failure so ``work --loop`` backs off calmly rather
    than treating an empty queue as an error storm.
    """


class ClaimTransportError(RuntimeError):
    """The claim board itself is unreachable/broken (network, auth, bad repo).

    Claims are cooperative ([COOP]): callers may log loudly and continue
    uncoordinated — safety comes from CAS pushes, never from claims.
    """
