"""Scoreboards and markers — machine-readable coordination state on PRs.

Three marker families (all HTML comments, invisible to humans reading the PR):

* ``<!--autoform-target:v1 {"node": "<id>"}-->`` in a PR *body* — the link from
  a PR to the graph node it proves (duplicate detection, avoid-lists, folding).
* ``<!--autoform-scoreboard-->`` + ``<!--autoform-meta:v1 {...}-->`` in a PR
  *comment* — the jury's verdict, scoped to a head SHA.
* ``<!--autoform-review-in-progress {...}-->`` in a PR *comment* — a
  self-expiring "I'm reviewing this head" marker so peers skip it.

TRUST MODEL: comments on a public repo are attacker-writable. Every parser here
that feeds a decision (``parse_meta``, ``active_inprogress``) therefore accepts
a ``trusted`` predicate over the comment author's login and ignores comments
from anyone else; callers pass a collaborator/identity check. Interpolated text
is sanitized so a malicious note can never smuggle a second marker, and the
machine meta is always the LAST marker in its comment — a marker injected into
an earlier field can never override it.
"""
from __future__ import annotations

import json
import time
from typing import Callable

from .constants import (
    INPROGRESS_MARK_RE,
    META_MARK_RE,
    REVIEW_INPROGRESS_TTL_S,
    SCOREBOARD_MARK,
    TARGET_MARK_RE,
)

_VERDICT_ICON = {"clean": "✅", "flagged": "🟡", "rejected": "⛔"}


def _sanitize(text: str) -> str:
    """Neutralize marker injection in interpolated text: an HTML-comment opener
    inside a note could otherwise plant a forged ``autoform-*`` marker."""
    return str(text).replace("<!--", "<!\N{ZERO WIDTH SPACE}--")


def _comment_login(comment: dict) -> str:
    user = comment.get("user")
    return str(user.get("login", "")) if isinstance(user, dict) else ""


def _last_marker_json(pattern, body: str):
    """The LAST well-formed marker payload in a body (injection-resistant:
    our writers always append the machine meta at the end)."""
    result = None
    for match in pattern.finditer(body or ""):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            result = data
    return result


# -- target marker (PR body) -------------------------------------------------

def format_target(node: str) -> str:
    return f'<!--autoform-target:v1 {json.dumps({"node": node})}-->'


def parse_target(body: str | None) -> str | None:
    """The node id a PR body claims to prove, or None."""
    data = _last_marker_json(TARGET_MARK_RE, body or "")
    node = data.get("node") if data else None
    return node if isinstance(node, str) and node else None


# -- scoreboard (PR comment) -------------------------------------------------

def format_scoreboard(
    node: str,
    head_sha: str,
    scores: dict,
    verdict: str,
    by: str,
    notes: dict | None = None,
) -> str:
    """One human-readable table + the machine meta, in a single comment.

    The meta marker is ALWAYS the final line — parsers take the last marker in
    a body, so nothing interpolated above it can override the verdict.
    """
    icon = _VERDICT_ICON.get(verdict, "❔")
    lines = [
        SCOREBOARD_MARK,
        f"## {icon} Autoform review — `{_sanitize(node)}`",
        "",
        f"Head: `{_sanitize(head_sha)[:12]}` · verdict: **{_sanitize(verdict)}** · reviewer: `{_sanitize(by)}`",
        "",
        "| axis | score |",
        "|---|---|",
    ]
    for axis in sorted(scores):
        value = scores[axis]
        lines.append(f"| {_sanitize(axis)} | {value if value is not None else '—'} |")
    for axis, note in sorted((notes or {}).items()):
        if note:
            lines += ["", f"**{_sanitize(axis)}:** {_sanitize(note)[:800]}"]
    meta = {
        "head_sha": head_sha,
        "node": node,
        "scores": scores,
        "verdict": verdict,
        "by": by,
        "at": int(time.time()),
    }
    lines += ["", f"<!--autoform-meta:v1 {json.dumps(meta, sort_keys=True)}-->"]
    return "\n".join(lines)


def parse_meta(
    comments: list[dict],
    trusted: Callable[[str], bool] | None = None,
    require_head: str | None = None,
) -> dict | None:
    """The newest scoreboard meta among issue comments (newest wins).

    ``trusted``: predicate over the comment author's login — comments from
    untrusted authors are ignored entirely (fail closed: with a predicate
    supplied, a comment with no author is untrusted). ``require_head``: when
    set, metas for any other head SHA are ignored.
    """
    newest: dict | None = None
    for comment in comments:
        if trusted is not None and not trusted(_comment_login(comment)):
            continue
        data = _last_marker_json(META_MARK_RE, comment.get("body") or "")
        if not data or not isinstance(data.get("head_sha"), str):
            continue
        if require_head is not None and data["head_sha"] != require_head:
            continue
        newest = data  # comments arrive oldest-first; keep the last
    return newest


# -- in-progress marker (PR comment) ----------------------------------------

def format_inprogress(head_sha: str, by: str, ttl: int = REVIEW_INPROGRESS_TTL_S) -> str:
    data = {"head": head_sha, "by": by, "expires_at": int(time.time()) + ttl}
    return f"<!--autoform-review-in-progress {json.dumps(data, sort_keys=True)}-->"


def active_inprogress(
    comments: list[dict],
    head_sha: str,
    now: float | None = None,
    trusted: Callable[[str], bool] | None = None,
) -> list[dict]:
    """Unexpired in-progress markers for exactly this head.

    ``trusted`` filters authors like :func:`parse_meta` — a random commenter
    must not be able to suppress reviews by planting fake markers.
    """
    now = now if now is not None else time.time()
    active: list[dict] = []
    for comment in comments:
        if trusted is not None and not trusted(_comment_login(comment)):
            continue
        data = _last_marker_json(INPROGRESS_MARK_RE, comment.get("body") or "")
        if (
            data is not None
            and data.get("head") == head_sha
            and isinstance(data.get("expires_at"), (int, float))
            and data["expires_at"] > now
        ):
            data["_comment_id"] = comment.get("id")
            active.append(data)
    return active
