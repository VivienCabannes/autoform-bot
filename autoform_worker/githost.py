"""GitHub host operations via the ``gh`` CLI — identity, repos, PRs, issues.

All network state flows through here so the survey/status paths are injectable
in tests (pass ``runner=`` to :class:`GitHost`). Secondary-rate-limit responses
are retried with bounded backoff; primary exhaustion surfaces immediately.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .errors import Die

_GH_TIMEOUT_S = 120
_SECONDARY_HINTS = ("secondary rate limit", "abuse detection", "was submitted too quickly")


def _default_runner(args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=_GH_TIMEOUT_S, input=input_text
    )


class GitHost:
    def __init__(self, runner=None, max_secondary_wait: int = 300):
        self._run = runner or _default_runner
        self._max_secondary_wait = max_secondary_wait
        self._me: str | None = None
        self._collab_cache: dict = {}

    def gh(self, args: list[str], check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
        waited = 0.0
        attempt = 0
        while True:
            proc = self._run(args, input_text)
            err = (proc.stderr or "").lower()
            if proc.returncode != 0 and any(h in err for h in _SECONDARY_HINTS) and waited < self._max_secondary_wait:
                delay = min(60 * (2 ** min(attempt, 3)), self._max_secondary_wait - waited)
                time.sleep(max(delay, 1))
                waited += delay
                attempt += 1
                continue
            if check and proc.returncode != 0:
                raise Die(f"gh {' '.join(args[:3])}… failed: {(proc.stderr or '').strip()[:500]}")
            return proc

    def gh_json(self, args: list[str], check: bool = True):
        proc = self.gh(args, check=check)
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as error:
            if check:
                raise Die(f"gh {' '.join(args[:3])}… returned invalid JSON: {error}") from error
            return None

    # -- identity / repos ---------------------------------------------------

    def me(self) -> str:
        if self._me is None:
            proc = self.gh(["api", "user", "--jq", ".login"], check=False)
            login = (proc.stdout or "").strip()
            if proc.returncode != 0 or not login:
                raise Die("gh is not authenticated — run `gh auth login` first")
            self._me = login
        return self._me

    def repo_info(self, slug: str) -> dict:
        data = self.gh_json([
            "repo", "view", slug, "--json",
            "nameWithOwner,defaultBranchRef,isFork,parent,hasIssuesEnabled,viewerPermission,visibility",
        ])
        if not isinstance(data, dict):
            raise Die(f"cannot view repo {slug}")
        return data

    def canonical_of(self, slug: str) -> tuple[str, str]:
        """Resolve a possibly-fork slug to (canonical slug, default branch).

        ``AUTOFORM_CANONICAL_REPO`` overrides discovery (for chains of forks).
        """
        import os
        override = os.environ.get("AUTOFORM_CANONICAL_REPO")
        if override:
            info = self.repo_info(override)
            return override, (info.get("defaultBranchRef") or {}).get("name", "main")
        info = self.repo_info(slug)
        parent = info.get("parent") or {}
        if info.get("isFork") and parent.get("owner", {}).get("login") and parent.get("name"):
            canonical = f'{parent["owner"]["login"]}/{parent["name"]}'
            cinfo = self.repo_info(canonical)
            return canonical, (cinfo.get("defaultBranchRef") or {}).get("name", "main")
        return slug, (info.get("defaultBranchRef") or {}).get("name", "main")

    def can_push(self, slug: str) -> bool:
        info = self.repo_info(slug)
        return info.get("viewerPermission") in {"ADMIN", "MAINTAIN", "WRITE"}

    def has_issues(self, slug: str) -> bool:
        return bool(self.repo_info(slug).get("hasIssuesEnabled"))

    def is_collaborator(self, slug: str, login: str) -> bool:
        """Whether ``login`` has repo access (the trust boundary for reviews and
        scoreboard folding). 204 = yes, 404/anything else = no; cached per host."""
        if not login:
            return False
        key = (slug, login)
        if key not in self._collab_cache:
            proc = self.gh(["api", f"/repos/{slug}/collaborators/{login}"], check=False)
            self._collab_cache[key] = proc.returncode == 0
        return self._collab_cache[key]

    def ensure_fork(self, canonical: str) -> str:
        """Find or create the operator's fork of ``canonical``; returns its slug."""
        me = self.me()
        owner_repo = canonical.split("/", 1)[1]
        candidate = f"{me}/{owner_repo}"
        info = self.gh_json(["repo", "view", candidate, "--json", "isFork,parent"], check=False)
        if isinstance(info, dict) and info.get("isFork"):
            parent = info.get("parent") or {}
            if f'{parent.get("owner", {}).get("login")}/{parent.get("name")}' == canonical:
                return candidate
        self.gh(["repo", "fork", canonical, "--clone=false"])
        for attempt in range(8):
            time.sleep(2 * (attempt + 1))
            info = self.gh_json(["repo", "view", candidate, "--json", "isFork"], check=False)
            if isinstance(info, dict):
                return candidate
        raise Die(f"forked {canonical} but {candidate} never appeared")

    # -- PRs ----------------------------------------------------------------

    PR_FIELDS = ("number,title,author,headRefName,headRefOid,headRepositoryOwner,"
                 "isDraft,mergeable,labels,body,statusCheckRollup,updatedAt,files")

    def pr_list(self, slug: str, state: str = "open", limit: int = 100, fields: str | None = None) -> list[dict]:
        data = self.gh_json([
            "pr", "list", "--repo", slug, "--state", state, "--limit", str(limit),
            "--json", fields or self.PR_FIELDS,
        ])
        return data if isinstance(data, list) else []

    def pr_comments(self, slug: str, number: int) -> list[dict]:
        data = self.gh_json([
            "api", "--paginate", f"/repos/{slug}/issues/{number}/comments?per_page=100",
        ], check=False)
        return data if isinstance(data, list) else []

    def post_comment(self, slug: str, number: int, body: str) -> None:
        self.gh(["api", "-X", "POST", f"/repos/{slug}/issues/{number}/comments",
                 "-f", f"body={body}"])

    def delete_comment(self, slug: str, comment_id: int) -> None:
        self.gh(["api", "-X", "DELETE", f"/repos/{slug}/issues/comments/{comment_id}"], check=False)

    def create_pr(self, slug: str, head: str, base: str, title: str, body_file: Path,
                  draft: bool = False, labels: list[str] | None = None) -> str:
        args = ["pr", "create", "--repo", slug, "--head", head, "--base", base,
                "--title", title, "--body-file", str(body_file)]
        if draft:
            args.append("--draft")
        proc = self.gh(args)
        url = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        if labels:
            number = url.rstrip("/").rsplit("/", 1)[-1]
            if number.isdigit():
                self.gh(["pr", "edit", number, "--repo", slug,
                         *[a for label in labels for a in ("--add-label", label)]], check=False)
        return url

    def merge_pr(self, slug: str, number: int, expect_head: str) -> bool:
        """Squash-merge a PR iff its head is still exactly ``expect_head`` —
        the merge-time CAS (``--match-head-commit``). False when the head moved
        or GitHub refuses (branch protection, conflicts); Die on transport."""
        proc = self.gh(["pr", "merge", str(number), "--repo", slug, "--squash",
                        "--match-head-commit", expect_head], check=False)
        return proc.returncode == 0

    # -- issues (escalation/intention sync; degrade when Issues are off) ----

    def issue_list(self, slug: str, label: str, state: str = "open") -> list[dict]:
        data = self.gh_json([
            "issue", "list", "--repo", slug, "--label", label, "--state", state,
            "--limit", "100", "--json", "number,title,body,assignees,state",
        ], check=False)
        return data if isinstance(data, list) else []

    def create_issue(self, slug: str, title: str, body: str, labels: list[str]) -> bool:
        proc = self.gh(["issue", "create", "--repo", slug, "--title", title, "--body", body,
                        *[a for label in labels for a in ("--label", label)]], check=False)
        return proc.returncode == 0

    def close_issue(self, slug: str, number: int, comment: str = "") -> None:
        args = ["issue", "close", str(number), "--repo", slug]
        if comment:
            args += ["--comment", comment]
        self.gh(args, check=False)

    def ensure_labels(self, slug: str, labels: list[str]) -> None:
        for label in labels:  # best-effort; exists-already errors are fine
            self.gh(["label", "create", label, "--repo", slug, "--force"], check=False)

    # -- budget -------------------------------------------------------------

    def rate_budget(self) -> dict:
        data = self.gh_json(["api", "rate_limit"], check=False)
        if not isinstance(data, dict):
            return {}
        out = {}
        for name in ("core", "graphql"):
            res = (data.get("resources") or {}).get(name) or {}
            out[name] = {"remaining": res.get("remaining"), "reset": res.get("reset")}
        return out


def build_state_of(pr: dict) -> str:
    """Derive one build state from ``statusCheckRollup``: failed | pending | success.

    No checks at all counts as ``success`` (small projects without CI still work).
    ``CANCELLED``/``ACTION_REQUIRED`` count as failed — neither is a green head.
    """
    rollup = pr.get("statusCheckRollup") or []
    saw_pending = False
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or check.get("state") or "").upper()
        if (conclusion in {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "CANCELLED", "ACTION_REQUIRED"}
                or status in {"FAILURE", "ERROR"}):
            return "failed"
        if conclusion in {"", "NEUTRAL"} and status in {"QUEUED", "IN_PROGRESS", "PENDING", "EXPECTED", "WAITING"}:
            saw_pending = True
    return "pending" if saw_pending else "success"
