# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Sync GitHub Issues rejection state into the autoform-bot DAG.

Closes the loop between the human-paced review CLI (which flips labels +
appends rejection notes to issue bodies) and the autonomous DAG worker
loop (which expects task descriptions + statuses to drive its iteration).

Polling design (one-shot or watched):

* List ``review:rejected`` open issues in the project repo via ``gh``.
* For each, extract the most recent rejection note from the issue body
  (the section appended by ``autoform review-reject``).
* Hash the note; if it differs from the last-synced hash recorded in
  ``<run_dir>/review_state/sync_log.json``, treat it as a new rejection.
* Update the corresponding DAG task: append the rejection note to the
  task description, reset status to pending, and write to the orchestrator
  inbox at ``<run_dir>/review_state/pending_rejections.json`` so the
  orchestrator picks it up on its next round.

The DAG-side write goes through a sidecar JSON file rather than directly
poking the running orchestrator's task tracker — this keeps the sync
command standalone (no MCP client connection), and the orchestrator's
prompt is updated to read the inbox at the start of each round.

When the run isn't active (no `<run_dir>/coordinator.pid`), the sync
still records the rejection in the log — the next ``autoform run`` will
pick it up on first orchestrator round.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoform.bot.tools.github_issues import GitHubIssuesOps

logger = logging.getLogger(__name__)

# The rejection-note header pattern written by ``review-reject``. We match
# the most recent one — there may be multiple if the issue cycled
# through review → reject → re-review → reject again.
_REJECTION_HEADER_RE = re.compile(
    r"^### Rejection note \(recorded ([^)]+)\)\s*$", re.MULTILINE
)

_LABEL_REJECTED = "review:rejected"


@dataclass
class RejectionRecord:
    """One pending rejection to surface to the orchestrator."""
    issue_number: int
    issue_title: str
    issue_url: str | None
    task_id: str | None  # filled when issue-number → task-id mapping known
    rejection_ts: str  # ISO 8601 from the issue body header
    rejection_hash: str  # sha256 of the rejection note
    rejection_note: str  # the actual demand text

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Rejection-note extraction
# ---------------------------------------------------------------------------


def extract_latest_rejection_note(body: str) -> tuple[str, str] | None:
    """Find the most recent ``### Rejection note (recorded …)`` section
    in an issue body and return ``(timestamp, note_text)``.

    Returns ``None`` if no rejection-note section is present.

    The note ends at the next H3 header (``### ``), the next ``---``
    horizontal rule, or end-of-body.
    """
    matches = list(_REJECTION_HEADER_RE.finditer(body))
    if not matches:
        return None
    last = matches[-1]
    ts = last.group(1).strip()
    body_after = body[last.end():]
    # Find the end of this section: next ### header or --- HR or EOF.
    end = len(body_after)
    next_header = re.search(r"^### ", body_after, re.MULTILINE)
    if next_header:
        end = min(end, next_header.start())
    next_hr = re.search(r"^---\s*$", body_after, re.MULTILINE)
    if next_hr:
        end = min(end, next_hr.start())
    return ts, body_after[:end].strip()


def hash_note(note: str) -> str:
    """Stable hash for change-detection."""
    return hashlib.sha256(note.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sync-log persistence
# ---------------------------------------------------------------------------


def _sync_log_path(run_dir: Path) -> Path:
    return run_dir / "review_state" / "sync_log.json"


def _pending_path(run_dir: Path) -> Path:
    return run_dir / "review_state" / "pending_rejections.json"


def load_sync_log(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Load the per-issue sync log. Returns empty dict on first run."""
    p = _sync_log_path(run_dir)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("could not read sync log at %s; starting fresh", p)
        return {}


def save_sync_log(run_dir: Path, log: dict[str, dict[str, Any]]) -> None:
    p = _sync_log_path(run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(log, indent=2))


def write_pending_inbox(run_dir: Path, records: list[RejectionRecord]) -> None:
    """Write the orchestrator's inbox file. Overwritten on each sync —
    the orchestrator reads it at the start of each round and is
    expected to clear handled entries."""
    p = _pending_path(run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rejections": [r.to_json() for r in records],
    }
    p.write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Issue-to-task mapping
# ---------------------------------------------------------------------------


def _load_task_for_issue(
    run_dir: Path, issue_number: int
) -> str | None:
    """Look up the DAG task id for a GitHub issue number.

    Inspects ``<run_dir>/tracker/tasks.json`` and matches against each
    task's ``metadata.github_issue_number`` field. Returns ``None`` if
    no task is mapped (e.g., the issue was created outside the
    bootstrap flow).
    """
    tracker_path = run_dir / "tracker" / "tasks.json"
    if not tracker_path.is_file():
        return None
    try:
        tasks: list[dict[str, Any]] = json.loads(tracker_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for task in tasks:
        meta = task.get("metadata") or {}
        if meta.get("github_issue_number") == issue_number:
            return task.get("id")
    return None


# ---------------------------------------------------------------------------
# Top-level sync
# ---------------------------------------------------------------------------


def sync_once(
    run_dir: Path,
    repo: str,
    ops: GitHubIssuesOps | None = None,
) -> list[RejectionRecord]:
    """Run one sync pass.

    Returns the list of rejections that are new since the last sync
    (by rejection-note hash). The sync log is updated and the
    orchestrator inbox is rewritten.
    """
    ops = ops or GitHubIssuesOps(default_repo=repo)
    sync_log = load_sync_log(run_dir)

    issues = ops.list_issues_by_label(_LABEL_REJECTED, state="open", repo=repo)
    new_records: list[RejectionRecord] = []
    all_records: list[RejectionRecord] = []

    for issue in issues:
        # Fetch the full issue (list returns metadata only, not body).
        full = ops.get_issue(issue.number, repo=repo)
        if full.body is None:
            logger.warning("issue #%d has no body; skipping", issue.number)
            continue
        extracted = extract_latest_rejection_note(full.body)
        if extracted is None:
            logger.info(
                "issue #%d has review:rejected label but no rejection-note "
                "section in the body; skipping (was it rejected without "
                "--notes?)", issue.number
            )
            continue
        ts, note = extracted
        h = hash_note(note)

        task_id = _load_task_for_issue(run_dir, issue.number)
        record = RejectionRecord(
            issue_number=issue.number,
            issue_title=issue.title,
            issue_url=issue.url,
            task_id=task_id,
            rejection_ts=ts,
            rejection_hash=h,
            rejection_note=note,
        )
        all_records.append(record)

        key = str(issue.number)
        prev = sync_log.get(key, {})
        if prev.get("rejection_hash") == h:
            continue  # already synced this rejection
        new_records.append(record)
        sync_log[key] = {
            "rejection_hash": h,
            "rejection_ts": ts,
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "task_id": task_id,
        }

    save_sync_log(run_dir, sync_log)
    write_pending_inbox(run_dir, all_records)
    return new_records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def sync_rejections(
    run_dir: str,
    repo: str | None = None,
    watch: bool = False,
    interval_s: int = 60,
) -> None:
    """Sync GitHub Issues rejection state into the DAG.

    Args:
        run_dir: Path to the autoform-bot run directory.
        repo: ``owner/name``; inferred via ``gh repo view`` if omitted.
        watch: If true, run continuously, polling every ``interval_s``.
            If false (default), run once and exit.
        interval_s: Poll interval in seconds when ``--watch`` is set.
            Default 60; matches marathon's daemon poll interval.
    """
    run_path = Path(run_dir).resolve()
    if not run_path.is_dir():
        raise RuntimeError(f"run-dir not found: {run_path}")
    if repo is None:
        proc = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "could not infer repo via `gh repo view`; pass --repo "
                "owner/name explicitly"
            )
        repo = proc.stdout.strip()

    ops = GitHubIssuesOps(default_repo=repo)

    def _tick() -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new = sync_once(run_path, repo, ops=ops)
        if new:
            print(f"[{ts}] {len(new)} new rejection(s) synced:")
            for r in new:
                mapped = f"→ task {r.task_id}" if r.task_id else "(unmapped)"
                print(f"  #{r.issue_number}: {r.issue_title} {mapped}")
        else:
            print(f"[{ts}] no new rejections")

    if not watch:
        _tick()
        return

    print(f"watch mode: polling every {interval_s}s. Ctrl-C to exit.")
    try:
        while True:
            _tick()
            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("\nstopped")
