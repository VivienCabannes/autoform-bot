# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Git operations — subprocess-based git commands.

No MCP dependencies.
"""

from __future__ import annotations

import os
import subprocess
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)


DEFAULT_MAX_OUTPUT = 50_000
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_LOG_COUNT = 20


class GitOps:
    """Git operations scoped to a repository directory."""

    def __init__(self, repo_dir: str, max_output: int = DEFAULT_MAX_OUTPUT, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.repo = str(Path(repo_dir).resolve())
        self.max_output = max_output
        self.timeout = timeout

    def _run(self, *args: str, env: dict[str, str] | None = None) -> str:
        try:
            run_env = {**os.environ, **env} if env else None
            result = subprocess.run(
                ["git", "-C", self.repo, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=run_env,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0 and not result.stderr:
                output += f"\n[exit code: {result.returncode}]"
            if len(output) > self.max_output:
                output = output[: self.max_output] + "\n\n[Output truncated]"
            return output if output.strip() else "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: git command timed out after {self.timeout}s"
        except Exception as e:
            return f"Error: {e}"

    # Read operations

    def status(self) -> str:
        return self._run("status")

    def diff(self, ref: str = "") -> str:
        args = ["diff"]
        if ref:
            args.append(ref)
        return self._run(*args)

    def log(self, max_count: int = DEFAULT_MAX_LOG_COUNT, oneline: bool = True) -> str:
        args = ["log", f"--max-count={max_count}"]
        if oneline:
            args.append("--oneline")
        return self._run(*args)

    def show(self, ref: str = "HEAD") -> str:
        return self._run("show", ref)

    def branch(self) -> str:
        return self._run("branch", "-a")

    def show_file(self, path: str, ref: str = "HEAD") -> str:
        return self._run("show", f"{ref}:{path}")

    # Write operations

    def add(self, paths: str = ".") -> str:
        return self._run("add", *paths.split())

    def commit(self, message: str) -> str:
        return self._run("commit", "-m", message)

    def checkout(self, ref: str) -> str:
        return self._run("checkout", ref)

    def restore(self, paths: str, staged: bool = False) -> str:
        args = ["restore"]
        if staged:
            args.append("--staged")
        args.extend(paths.split())
        return self._run(*args)

    def reset(self, ref: str = "HEAD", paths: str = "") -> str:
        args = ["reset", ref]
        if paths:
            args.extend(["--", *paths.split()])
        return self._run(*args)

    # Rebase workflow

    def rebase(self, branch: str = "main") -> str:
        return self._run("rebase", branch, env={"GIT_EDITOR": "true"})

    def rebase_continue(self) -> str:
        return self._run("rebase", "--continue", env={"GIT_EDITOR": "true"})

    def rebase_abort(self) -> str:
        return self._run("rebase", "--abort")

    def rebase_skip(self) -> str:
        return self._run("rebase", "--skip")

    def conflicts(self) -> str:
        return self._run("diff", "--check")
