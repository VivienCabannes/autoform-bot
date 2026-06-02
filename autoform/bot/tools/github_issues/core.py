# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""GitHub Issues operations — thin wrapper over the ``gh`` CLI.

Why ``gh`` instead of PyGithub: the user is already authenticated via
``gh auth login`` (matches how the rest of autoform-bot's git interactions
work), and ``gh`` handles pagination + rate limits transparently. The
trade-off is that we shell out per call instead of holding a session, but
the call rate from human-paced review is low (single-digit calls per
minute), so the per-call latency is fine.

All ops take an explicit ``repo`` argument (``owner/name``) so a single
``GitHubIssuesOps`` instance can target multiple repositories — useful in
multi-book runs where each book has its own GitHub project. The repo is
threaded through tools as a constructor-time default for the common case
of a single repository per run.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class GitHubIssuesError(RuntimeError):
    """Raised when a ``gh`` invocation fails. The cause's stderr is
    propagated unmodified for upstream surfacing."""


@dataclass
class IssueRef:
    """Lightweight record returned by list/get ops."""
    number: int
    title: str
    state: str  # OPEN, CLOSED
    labels: list[str]
    body: str | None = None  # populated by get_issue, not by list_issues
    url: str | None = None


class GitHubIssuesOps:
    """Stateless operations against a GitHub repo's Issues API.

    The default repo passed to the constructor is used when callers omit
    ``repo`` on individual calls. All methods raise ``GitHubIssuesError``
    on ``gh`` failure; readers should catch and surface to the agent.
    """

    def __init__(self, default_repo: str | None = None) -> None:
        self.default_repo = default_repo

    # --- helpers ------------------------------------------------------

    def _resolve_repo(self, repo: str | None) -> str:
        chosen = repo or self.default_repo
        if chosen is None:
            raise GitHubIssuesError(
                "no repo specified and no default_repo set on GitHubIssuesOps"
            )
        return chosen

    def _gh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run ``gh`` with the given args. Captures stdout + stderr. Logs
        the exit code at INFO. Raises ``GitHubIssuesError`` on non-zero
        exit when ``check=True``."""
        cmd = ["gh", *args]
        logger.debug("running %s", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 and check:
            raise GitHubIssuesError(
                f"gh {' '.join(args[:3])}... failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )
        return proc

    # --- create + edit ------------------------------------------------

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        repo: str | None = None,
    ) -> int:
        """Create an issue. Returns the new issue number.

        Labels are added in the same call so the issue is never visible
        without its taxonomy. ``--json number`` lets us avoid parsing the
        human-readable URL output.
        """
        args = [
            "issue", "create",
            "--repo", self._resolve_repo(repo),
            "--title", title,
            "--body", body,
        ]
        for label in labels or []:
            args.extend(["--label", label])
        proc = self._gh(*args)
        # `gh issue create` prints the issue URL to stdout, e.g.
        # https://github.com/owner/repo/issues/42 — extract the number.
        url = proc.stdout.strip().splitlines()[-1]
        try:
            return int(url.rsplit("/", 1)[-1])
        except (ValueError, IndexError) as e:
            raise GitHubIssuesError(
                f"could not parse issue number from gh output: {url!r}"
            ) from e

    def update_issue_body(
        self,
        number: int,
        body: str,
        repo: str | None = None,
    ) -> None:
        """Replace the issue body."""
        # gh accepts --body-file - to read from stdin, which avoids ARG_MAX
        # issues on long bodies (marathon's reject notes can be ~5KB).
        cmd = [
            "gh", "issue", "edit", str(number),
            "--repo", self._resolve_repo(repo),
            "--body-file", "-",
        ]
        proc = subprocess.run(
            cmd, input=body, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise GitHubIssuesError(
                f"gh issue edit (body) failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )

    def add_label(
        self,
        number: int,
        label: str,
        repo: str | None = None,
    ) -> None:
        """Add a label to an issue (no-op if already present)."""
        self._gh(
            "issue", "edit", str(number),
            "--repo", self._resolve_repo(repo),
            "--add-label", label,
        )

    def remove_label(
        self,
        number: int,
        label: str,
        repo: str | None = None,
    ) -> None:
        """Remove a label from an issue (no-op if not present)."""
        self._gh(
            "issue", "edit", str(number),
            "--repo", self._resolve_repo(repo),
            "--remove-label", label,
        )

    # --- read ---------------------------------------------------------

    def get_issue(
        self,
        number: int,
        repo: str | None = None,
    ) -> IssueRef:
        """Fetch a single issue including body."""
        proc = self._gh(
            "issue", "view", str(number),
            "--repo", self._resolve_repo(repo),
            "--json", "number,title,state,labels,body,url",
        )
        data = json.loads(proc.stdout)
        return IssueRef(
            number=data["number"],
            title=data["title"],
            state=data["state"],
            labels=[lbl["name"] for lbl in data.get("labels", [])],
            body=data.get("body"),
            url=data.get("url"),
        )

    def list_issues_by_label(
        self,
        label: str,
        state: str = "open",
        repo: str | None = None,
        limit: int = 200,
    ) -> list[IssueRef]:
        """Return all issues with ``label`` in the given state.

        ``state`` is one of ``open``, ``closed``, ``all``. The default
        limit (200) covers typical chapter-scale workflows; raise it
        only when paginating across many chapters.
        """
        proc = self._gh(
            "issue", "list",
            "--repo", self._resolve_repo(repo),
            "--label", label,
            "--state", state,
            "--limit", str(limit),
            "--json", "number,title,state,labels,url",
        )
        rows = json.loads(proc.stdout)
        return [
            IssueRef(
                number=r["number"],
                title=r["title"],
                state=r["state"],
                labels=[lbl["name"] for lbl in r.get("labels", [])],
                url=r.get("url"),
            )
            for r in rows
        ]

    def comment(
        self,
        number: int,
        body: str,
        repo: str | None = None,
    ) -> None:
        """Post a comment on an issue. Used by the rejection-sync flow
        to acknowledge that a rejection was accepted into the DAG."""
        cmd = [
            "gh", "issue", "comment", str(number),
            "--repo", self._resolve_repo(repo),
            "--body-file", "-",
        ]
        proc = subprocess.run(
            cmd, input=body, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise GitHubIssuesError(
                f"gh issue comment failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )

    # --- sub-issues + parent tracker --------------------------------

    def link_as_subissue(
        self,
        child_number: int,
        parent_number: int,
        repo: str | None = None,
    ) -> None:
        """Attach ``child_number`` as a sub-issue of ``parent_number``.

        Uses the GitHub REST API's sub-issues endpoint (beta as of late
        2025; if unavailable on the repo, this raises and the caller
        falls back to no-op + a manual parent-body edit).
        """
        # The sub-issues API takes the child issue's *internal id*, not
        # its number. Fetch it first.
        proc = self._gh(
            "api", f"repos/{self._resolve_repo(repo)}/issues/{child_number}",
            "--jq", ".id",
        )
        try:
            child_id = int(proc.stdout.strip())
        except ValueError as e:
            raise GitHubIssuesError(
                f"could not resolve internal id for issue #{child_number}: "
                f"{proc.stdout!r}"
            ) from e
        self._gh(
            "api", "-X", "POST",
            f"repos/{self._resolve_repo(repo)}/issues/{parent_number}/sub_issues",
            "-f", f"sub_issue_id={child_id}",
            check=True,
        )


# --- convenience: extract Lean declarations from issue bodies ----------
#
# Used by the verified-decl audit (separate module) and by the coreviewer
# CLI when constructing per-issue prompts. Mirrors marathon's
# verified_decls.py extraction with the same DECL_KEYWORDS set, kept as a
# helper here so the audit module can use it without importing the whole
# tool server.

import re  # noqa: E402  (kept module-end to colocate with helper)

_DECL_KEYWORDS = (
    "def", "theorem", "lemma", "abbrev", "instance", "structure",
    "class", "inductive", "opaque", "axiom",
)
_DECL_RE = re.compile(
    r"^\s*"
    + r"(?:@\[[^\]]*\]\s*)*"
    + r"(?:noncomputable\s+)?(?:private\s+)?(?:protected\s+)?"
    + r"(?:" + "|".join(_DECL_KEYWORDS) + r")\s+"
    + r"(?P<name>[A-Za-z_][\w'.]*)",
    re.MULTILINE,
)
_LEAN_BLOCK_RE = re.compile(r"```lean\n(.*?)\n```", re.DOTALL)


def extract_lean_declarations(body: str) -> set[str]:
    """Parse ` ```lean ... ``` ` code blocks in an issue body and return
    the set of declaration names found. Anonymous declarations (e.g.,
    unnamed ``instance``) are excluded."""
    out: set[str] = set()
    for m in _LEAN_BLOCK_RE.finditer(body):
        for d in _DECL_RE.finditer(m.group(1)):
            out.add(d.group("name"))
    return out


def issue_to_dict(issue: IssueRef) -> dict[str, Any]:
    """Serialize an ``IssueRef`` to a plain dict for JSON tool output."""
    return {
        "number": issue.number,
        "title": issue.title,
        "state": issue.state,
        "labels": issue.labels,
        "url": issue.url,
        **({"body": issue.body} if issue.body is not None else {}),
    }
