# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""CLI subcommands for human-paced per-task review.

Three commands, exposed via ``fire`` in ``main.py``:

* ``autoform review-open <issue_number>`` — launches a Claude Code chat
  in VS Code (via ``vscode://anthropic.claude-code/open?prompt=...``)
  pre-loaded with the human-coreviewer agent's workflow against a
  specific GitHub sub-issue. Optionally takes a ``--task-id`` to also
  pull the DAG task description into the prompt.

* ``autoform review-verify <issue_number>`` — applies the
  ``review:verified`` label and strips ``review:rejected`` if present.
  Marathon's verify analog. Does NOT modify the DAG directly; the
  rejection-sync poller picks up label changes on its next tick.

* ``autoform review-reject <issue_number> --notes "..."`` — appends a
  rejection-note section to the issue body and applies
  ``review:rejected``, stripping ``review:verified``. Marathon's reject
  analog. The rejection-sync poller picks up the label change and
  feeds the notes back into the DAG task description, resetting status
  to pending.

The three commands are intentionally thin: they only talk to GitHub
Issues + the local DAG state JSON (when given a run_dir). The
substantive workflow lives in the coreviewer agent prompt; this module
is just the launcher + label-state convenience.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoform.bot.tools.github_issues import GitHubIssuesOps

logger = logging.getLogger(__name__)

# VS Code URI handler ceiling. Marathon hit this empirically; the
# coreviewer prompt template is sized to leave ~250 chars of headroom.
_URI_PROMPT_CEILING = 5000

# Labels managed by this CLI. The label names are kept in sync with
# marathon's vocabulary so cross-tool issue browsing works without
# translation.
_LABEL_VERIFIED = "review:verified"
_LABEL_REJECTED = "review:rejected"


# ---------------------------------------------------------------------------
# review-open
# ---------------------------------------------------------------------------


def _build_coreview_prompt(
    issue_number: int,
    repo: str,
    issue_body_preview: str,
    task_id: str | None,
    task_description: str | None,
) -> str:
    """Construct the URI-handler prompt for the human-coreviewer agent.

    Compact by design — the substantive workflow lives in the agent's
    `prompt.md`; this just briefs the agent on which task to focus on.
    """
    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    parts = [
        f"You are the human-coreviewer for autoform-bot. Follow the "
        f"six-step workflow in your system prompt. The task in focus "
        f"is GitHub sub-issue [#{issue_number}]({issue_url}) on "
        f"`{repo}`."
    ]
    if task_id is not None:
        parts.append(f"DAG task id: `{task_id}`.")
    if task_description:
        # Truncate to leave headroom for the issue body preview + agent
        # workflow recap.
        desc = task_description[:1200]
        if len(task_description) > 1200:
            desc += "… (truncated; full description in DAG)"
        parts.append(f"## Task description\n\n{desc}")
    if issue_body_preview:
        preview = issue_body_preview[:1800]
        if len(issue_body_preview) > 1800:
            preview += "… (truncated; full body via `get_issue` tool)"
        parts.append(f"## Issue body (current)\n\n{preview}")
    parts.append(
        "Begin step 1 of your workflow: read the issue + cited code, "
        "surface drift, and wait for the human's acknowledgment before "
        "moving to step 2. Do NOT apply any verdict — recommendations "
        "only; the human runs `autoform review-verify` or "
        "`review-reject` from the terminal."
    )
    return "\n\n".join(parts)


def _launch_vscode_uri(prompt: str) -> None:
    """Open the VS Code URI for Claude Code with the given prompt.

    Uses ``open`` on macOS, ``xdg-open`` on Linux, ``start`` on Windows.
    Falls back to printing the URI for manual paste.
    """
    encoded = urllib.parse.quote(prompt, safe="")
    uri = f"vscode://anthropic.claude-code/open?prompt={encoded}"
    if len(uri) > _URI_PROMPT_CEILING + 200:
        # The +200 covers the scheme + path overhead beyond the prompt
        # itself; the ceiling check below catches the prompt-size
        # problem with a clearer message.
        pass
    platform = sys.platform
    if platform == "darwin":
        opener = ["open", uri]
    elif platform.startswith("linux"):
        opener = ["xdg-open", uri]
    elif platform.startswith("win"):
        opener = ["cmd", "/c", "start", "", uri]
    else:
        print(f"Unsupported platform {platform!r}; open this URI manually:")
        print(uri)
        return
    try:
        subprocess.run(opener, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Could not launch VS Code via URI ({e}); open this manually:")
        print(uri)


def review_open(
    issue_number: int,
    repo: str | None = None,
    task_id: str | None = None,
    run_dir: str | None = None,
    dry_run: bool = False,
) -> None:
    """Launch a Claude Code chat in VS Code for human-paced review of a
    GitHub sub-issue.

    Args:
        issue_number: GitHub issue number to review.
        repo: ``owner/name`` of the GitHub repo. If omitted, inferred
            from ``gh repo view`` in the current working directory.
        task_id: DAG task id, if you want the prompt to include the
            task description. Optional.
        run_dir: Path to the autoform-bot run directory, used to look
            up the task description when ``--task-id`` is given.
            Optional.
        dry_run: Print the URI prompt instead of launching VS Code.
    """
    repo = repo or _infer_default_repo()
    ops = GitHubIssuesOps(default_repo=repo)
    issue = ops.get_issue(issue_number)
    print(f"  issue #{issue_number}: {issue.title}")
    print(f"  labels: {', '.join(issue.labels) if issue.labels else '(none)'}")

    task_description = None
    if task_id is not None and run_dir is not None:
        task_description = _load_task_description(Path(run_dir), task_id)
        if task_description is None:
            print(f"  warning: task {task_id} not found in {run_dir}; "
                  "prompt will omit the task description")

    prompt = _build_coreview_prompt(
        issue_number=issue_number,
        repo=repo,
        issue_body_preview=issue.body or "",
        task_id=task_id,
        task_description=task_description,
    )
    print(f"  prompt: {len(prompt)} / {_URI_PROMPT_CEILING} chars")
    if len(prompt) > _URI_PROMPT_CEILING:
        print("  warning: prompt exceeds VS Code URI ceiling; truncating "
              "task description / issue body if present.")

    if dry_run:
        print("\n--- prompt ---")
        print(prompt)
        return

    _launch_vscode_uri(prompt)
    print(f"  opened interactive Claude Code session for #{issue_number}")


def _infer_default_repo() -> str:
    """Run ``gh repo view --json nameWithOwner`` to infer the repo from
    the current working directory."""
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Could not infer repo via `gh repo view`. Pass --repo "
            "owner/name explicitly."
        )
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# review-verify
# ---------------------------------------------------------------------------


def review_verify(
    issue_number: int,
    repo: str | None = None,
    no_comment: bool = False,
) -> None:
    """Mark a sub-issue as VERIFIED.

    Adds ``review:verified`` label, strips ``review:rejected`` if
    present, and optionally posts a "verified at <ts>" comment so the
    issue's comment thread is a verdict log.

    Args:
        issue_number: GitHub issue number.
        repo: ``owner/name``; inferred via ``gh repo view`` if omitted.
        no_comment: Skip the verdict-log comment.
    """
    repo = repo or _infer_default_repo()
    ops = GitHubIssuesOps(default_repo=repo)
    print(f"Marking #{issue_number} as VERIFIED on {repo}...")
    ops.add_label(issue_number, _LABEL_VERIFIED)
    ops.remove_label(issue_number, _LABEL_REJECTED)
    if not no_comment:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ops.comment(issue_number, f"Verified at {ts} via `autoform review-verify`.")
    print(f"  + label {_LABEL_VERIFIED}")
    print(f"  - label {_LABEL_REJECTED} (no-op if not present)")


# ---------------------------------------------------------------------------
# review-reject
# ---------------------------------------------------------------------------


def review_reject(
    issue_number: int,
    notes: str | None = None,
    notes_file: str | None = None,
    repo: str | None = None,
    no_body_append: bool = False,
) -> None:
    """Mark a sub-issue as REJECTED with rejection notes.

    Appends a rejection-note section to the issue body (so the next
    worker iteration sees the demands) and applies the
    ``review:rejected`` label.

    Args:
        issue_number: GitHub issue number.
        notes: Rejection notes as a string. One of ``notes`` or
            ``notes_file`` is required.
        notes_file: Path to a file containing the rejection notes
            (markdown). Use this for multi-paragraph notes; ``notes``
            is fine for one-liners.
        repo: ``owner/name``; inferred via ``gh repo view`` if omitted.
        no_body_append: Skip appending the notes to the issue body
            (apply the label only). Use when the human prefers to
            update the body manually.
    """
    if notes is None and notes_file is None:
        raise ValueError("one of --notes or --notes-file is required")
    if notes_file is not None:
        notes = Path(notes_file).read_text()
    assert notes is not None  # narrowing for type checker

    repo = repo or _infer_default_repo()
    ops = GitHubIssuesOps(default_repo=repo)
    print(f"Marking #{issue_number} as REJECTED on {repo}...")

    if not no_body_append:
        existing = ops.get_issue(issue_number)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        appended = (
            (existing.body or "")
            + f"\n\n---\n\n### Rejection note (recorded {ts})\n\n"
            + notes.rstrip()
            + "\n"
        )
        ops.update_issue_body(issue_number, appended)
        print(f"  body: appended {len(notes)} chars of rejection notes")

    ops.add_label(issue_number, _LABEL_REJECTED)
    ops.remove_label(issue_number, _LABEL_VERIFIED)
    print(f"  + label {_LABEL_REJECTED}")
    print(f"  - label {_LABEL_VERIFIED} (no-op if not present)")
    print(
        "  Note: rejection-sync (running in the daemon) will pick this "
        "up on its next poll, append the notes to the DAG task "
        "description, and reset task status to pending."
    )


# ---------------------------------------------------------------------------
# Helpers shared with rejection_sync
# ---------------------------------------------------------------------------


def _load_task_description(run_dir: Path, task_id: str) -> str | None:
    """Load a task's description from the run's tracker JSON.

    The tracker writes to ``<run_dir>/tracker/tasks.json``; that file
    is a list of dicts with ``id``, ``title``, ``description``, etc.
    Returns ``None`` if the file doesn't exist or the task isn't
    found.
    """
    import json
    tracker_path = run_dir / "tracker" / "tasks.json"
    if not tracker_path.is_file():
        return None
    try:
        tasks: list[dict[str, Any]] = json.loads(tracker_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for task in tasks:
        if task.get("id") == task_id:
            return task.get("description")
    return None
