"""Run one side of Git's local smart transport from a pinned directory FD."""

from __future__ import annotations

import os
import resource
import sys


def main() -> None:
    if len(sys.argv) >= 7 and sys.argv[1] == "run-limited":
        try:
            directory_fd = int(sys.argv[2])
            file_bytes = int(sys.argv[3])
            cpu_seconds = int(sys.argv[4])
            open_files = int(sys.argv[5])
            for limit, requested in (
                (resource.RLIMIT_FSIZE, file_bytes),
                (resource.RLIMIT_CPU, cpu_seconds),
                (resource.RLIMIT_NOFILE, open_files),
                (resource.RLIMIT_CORE, 0),
            ):
                _soft, hard = resource.getrlimit(limit)
                bounded = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
                resource.setrlimit(limit, (bounded, bounded))
            os.fchdir(directory_fd)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"could not enter bounded Git quarantine: {exc}") from exc
        os.execvp(sys.argv[6], sys.argv[6:])
    if len(sys.argv) < 3 or sys.argv[1] not in {"upload", "receive"}:
        raise SystemExit(
            "usage: _git_fd_transport.py {upload|receive} DIRECTORY_FD | "
            "run-limited DIRECTORY_FD FILE_BYTES CPU_SECONDS OPEN_FILES COMMAND ..."
        )
    mode = sys.argv[1]
    try:
        directory_fd = int(sys.argv[2])
        os.fchdir(directory_fd)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"could not enter pinned Git repository: {exc}") from exc
    command = ["git"]
    if mode == "upload":
        command.extend(["-c", "uploadpack.allowFilter=true"])
    command.extend([f"{mode}-pack", "."])
    os.execvp("git", command)


if __name__ == "__main__":
    main()
