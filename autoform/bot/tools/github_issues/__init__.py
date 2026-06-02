# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP tool server for GitHub Issues, used to mirror DAG task state ↔ GH
sub-issues for human-paced per-declaration review.

The bridge enables marathon-style workflows on top of autoform-bot's DAG:
each first-class DAG task can be backed by a GitHub sub-issue, which the
human reviewer uses as the per-task UI (verify/reject labels, comment
threads, parent-tracker linking). Status sync from DAG → GitHub is
explicit (tools below); the reverse direction lives in a separate
`rejection_sync` module that polls for `review:rejected` label additions.
"""

from .core import GitHubIssuesOps
from .server import create_github_issues_server, github_issues_server

__all__ = [
    "GitHubIssuesOps",
    "create_github_issues_server",
    "github_issues_server",
]
