"""Compatibility exports for Autoform's canonical fail-closed claim board.

The distributed worker and the Markdown tooling share one lease protocol.  Keep
this module so existing worker imports remain stable while all behavior lives in
``autoform_cli.claims``.
"""

from autoform_cli.claims import (
    CLAIM_HEARTBEAT_S,
    CLAIM_KEY_RE,
    CLAIM_REF_PREFIX,
    CLAIM_SCHEMA,
    CLAIM_TTL_S,
    ClaimBoard,
    ClaimTransportError,
    Heartbeat,
    MalformedLeaseError,
    author_claim_key,
)

__all__ = [
    "CLAIM_HEARTBEAT_S",
    "CLAIM_KEY_RE",
    "CLAIM_REF_PREFIX",
    "CLAIM_SCHEMA",
    "CLAIM_TTL_S",
    "ClaimBoard",
    "ClaimTransportError",
    "Heartbeat",
    "MalformedLeaseError",
    "author_claim_key",
]
