# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP tool server exposing :class:`GitHubIssuesOps` to agents.

Reads (list, get) are autonomy=READ; writes (create, edit, label) are
autonomy=EXECUTE. The constrained design mirrors ``task_tracker``:
state-changing tools surface clear error strings rather than raising,
so the calling agent can recover gracefully.
"""

from __future__ import annotations

import json

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import (
    GitHubIssuesError,
    GitHubIssuesOps,
    issue_to_dict,
)


def create_github_issues_server(ops: GitHubIssuesOps) -> FastMCP:
    """Create an inprocess FastMCP server wrapping a GitHubIssuesOps."""
    server = FastMCP(name="github-issues")

    # ------------------------------------------------------------------
    # Read-only tools
    # ------------------------------------------------------------------

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_issues_by_label(
        label: str,
        state: str = "open",
        repo: str | None = None,
        limit: int = 200,
    ) -> str:
        """List issues with a given label.

        Args:
            label: Label name to filter on, e.g. ``review:verified``.
            state: ``open`` (default), ``closed``, or ``all``.
            repo: ``owner/name`` override; uses the default if omitted.
            limit: Max issues returned. Default 200.
        """
        try:
            issues = ops.list_issues_by_label(label, state=state, repo=repo, limit=limit)
        except GitHubIssuesError as e:
            return f"Error: {e}"
        if not issues:
            return f"No issues with label {label!r} in state {state!r}."
        return json.dumps([issue_to_dict(i) for i in issues], indent=2)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_issue(number: int, repo: str | None = None) -> str:
        """Fetch one issue including its body.

        Args:
            number: Issue number.
            repo: ``owner/name`` override; uses the default if omitted.
        """
        try:
            issue = ops.get_issue(number, repo=repo)
        except GitHubIssuesError as e:
            return f"Error: {e}"
        return json.dumps(issue_to_dict(issue), indent=2)

    # ------------------------------------------------------------------
    # Mutation tools
    # ------------------------------------------------------------------

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def create_issue(
        title: str,
        body: str,
        labels: list[str] | None = None,
        parent_number: int | None = None,
        repo: str | None = None,
    ) -> str:
        """Create a new issue, optionally attaching it as a sub-issue.

        Args:
            title: Issue title.
            body: Issue body (markdown).
            labels: Labels to apply on creation, e.g. ``["review", "chapter-14"]``.
            parent_number: If provided, attach the new issue as a sub-issue
              of this parent. If the sub-issues API isn't available on the
              repo, the link step is skipped and a note is appended to the
              returned message.
            repo: ``owner/name`` override; uses the default if omitted.
        """
        try:
            num = ops.create_issue(title, body, labels=labels, repo=repo)
        except GitHubIssuesError as e:
            return f"Error: {e}"
        msg = f"Created issue #{num}: {title}"
        if parent_number is not None:
            try:
                ops.link_as_subissue(num, parent_number, repo=repo)
                msg += f" (linked under #{parent_number})"
            except GitHubIssuesError as e:
                msg += f" (created but sub-issue link failed: {e})"
        return msg

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def update_issue_body(
        number: int,
        body: str,
        repo: str | None = None,
    ) -> str:
        """Replace the body of an existing issue.

        Args:
            number: Issue number to update.
            body: New body markdown (replaces the old body entirely).
            repo: ``owner/name`` override; uses the default if omitted.
        """
        try:
            ops.update_issue_body(number, body, repo=repo)
        except GitHubIssuesError as e:
            return f"Error: {e}"
        return f"Updated body of issue #{number}."

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def add_label(
        number: int,
        label: str,
        repo: str | None = None,
    ) -> str:
        """Add a label to an issue.

        Args:
            number: Issue number.
            label: Label name (e.g. ``review:verified``).
            repo: ``owner/name`` override; uses the default if omitted.
        """
        try:
            ops.add_label(number, label, repo=repo)
        except GitHubIssuesError as e:
            return f"Error: {e}"
        return f"Added label {label!r} to issue #{number}."

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def remove_label(
        number: int,
        label: str,
        repo: str | None = None,
    ) -> str:
        """Remove a label from an issue.

        Args:
            number: Issue number.
            label: Label name to remove.
            repo: ``owner/name`` override; uses the default if omitted.
        """
        try:
            ops.remove_label(number, label, repo=repo)
        except GitHubIssuesError as e:
            return f"Error: {e}"
        return f"Removed label {label!r} from issue #{number}."

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def comment(
        number: int,
        body: str,
        repo: str | None = None,
    ) -> str:
        """Post a comment on an issue.

        Args:
            number: Issue number.
            body: Comment body (markdown).
            repo: ``owner/name`` override; uses the default if omitted.
        """
        try:
            ops.comment(number, body, repo=repo)
        except GitHubIssuesError as e:
            return f"Error: {e}"
        return f"Commented on issue #{number}."

    return server


def github_issues_server(default_repo: str | None = None) -> MCPServerConfig:
    """Create an inprocess MCPServerConfig for the GitHub Issues bridge.

    Args:
        default_repo: ``owner/name`` of the repo most calls target. Per-call
            ``repo`` overrides are still allowed.
    """
    ops = GitHubIssuesOps(default_repo=default_repo)
    return MCPServerConfig(
        server_key="github-issues",
        description=(
            "Mirror DAG tasks to GitHub Issues: create, label, body-edit, "
            "comment, and list sub-issues by label."
        ),
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_github_issues_server(ops),
    )
