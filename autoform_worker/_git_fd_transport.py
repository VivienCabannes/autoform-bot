"""Run a local Git transport against an already-open repository directory."""

from __future__ import annotations

import os
import stat
import sys


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in {"upload", "receive"}:
        return 2
    try:
        descriptor = int(sys.argv[2])
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            return 2
        os.fchdir(descriptor)
        for key in (
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        ):
            os.environ.pop(key, None)
        executable = "git-upload-pack" if sys.argv[1] == "upload" else "git-receive-pack"
        os.execvp(executable, [executable, "."])
    except (OSError, ValueError):
        return 2
    return 2  # pragma: no cover - os.execvp does not return on success


if __name__ == "__main__":  # pragma: no cover - exercised through Git
    raise SystemExit(main())
