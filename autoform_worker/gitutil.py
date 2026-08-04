"""Git plumbing — the [HARD] safety layer.

Every branch write the worker makes goes through :func:`safe_push`, a
compare-and-swap ``git push --force-with-lease=<ref>:<expected-oid>``: GitHub's
atomic ref update guarantees exactly one writer wins, so nothing the cooperative
claim layer gets wrong can corrupt a branch. An empty expected OID means
*create-only* (the ref must not exist yet).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import Die

_GIT_TIMEOUT_S = 300


def run_git(
    args: list[str],
    cwd: Path | str | None = None,
    check: bool = True,
    timeout: int = _GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise Die(f"git {' '.join(args[:3])}… failed (rc={proc.returncode}): {proc.stderr.strip()[:500]}")
    return proc


def is_git_repo(path: Path) -> bool:
    proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def origin_url(repo: Path, remote: str = "origin") -> str | None:
    proc = run_git(["remote", "get-url", remote], cwd=repo, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def parse_slug(url: str) -> str | None:
    """``owner/repo`` from an https/ssh GitHub remote URL, else None."""
    url = url.strip()
    for prefix in ("git@github.com:", "ssh://git@github.com/", "https://github.com/", "http://github.com/"):
        if url.startswith(prefix):
            tail = url[len(prefix):].strip("/")
            if tail.endswith(".git"):
                tail = tail[:-4]
            parts = tail.split("/")
            if len(parts) == 2 and all(parts):
                return "/".join(parts)
    return None


def slug_url(slug: str) -> str:
    """Push/fetch URL for an ``owner/repo`` slug.

    ``AUTOFORM_GIT_BASE_URL`` overrides the host (GitHub Enterprise, or a local
    path prefix in tests — any git-clonable base works).
    """
    import os

    base = os.environ.get("AUTOFORM_GIT_BASE_URL", "https://github.com").rstrip("/")
    return f"{base}/{slug}"


def head_oid(repo: Path, ref: str = "HEAD") -> str:
    return run_git(["rev-parse", ref], cwd=repo).stdout.strip()


def current_branch(repo: Path) -> str:
    """The current branch name, or the literal ``HEAD`` when detached."""
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()


def clean_tree(repo: Path) -> bool:
    proc = run_git(["status", "--porcelain", "--untracked-files=all"], cwd=repo)
    return not proc.stdout.strip()


def remote_ref_oid(url: str, ref: str) -> str | None:
    """The OID a remote ref points at (``git ls-remote``), or None if absent."""
    proc = run_git(["ls-remote", url, ref], check=False, timeout=120)
    if proc.returncode != 0:
        raise Die(f"ls-remote {url} failed: {proc.stderr.strip()[:300]}")
    line = proc.stdout.strip()
    return line.split("\t", 1)[0] if line else None


def fetch(repo: Path, remote: str, *refspecs: str) -> None:
    run_git(["fetch", remote, *refspecs], cwd=repo, timeout=600)


def safe_push(
    repo: Path,
    ref: str,
    *,
    remote: str,
    expect: str | None,
    local: str = "HEAD",
) -> bool:
    """CAS-push ``local`` to ``refs/heads/<ref>`` on ``remote``.

    ``expect``: the remote OID observed before the work started (the lease).
    ``None`` or ``""`` means create-only — the push fails if the ref exists.
    Returns True on success, False when the CAS lost (someone else pushed);
    raises :class:`Die` on transport/auth errors.
    """
    full_ref = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
    lease = f"{full_ref}:{expect or ''}"
    proc = run_git(
        ["push", f"--force-with-lease={lease}", remote, f"{local}:{full_ref}"],
        cwd=repo, check=False, timeout=600,
    )
    if proc.returncode == 0:
        return False if _push_noop(proc) else True
    err = (proc.stderr or "") + (proc.stdout or "")
    # Every server/client shape of "the CAS lost" is a False, never a Die:
    # client-side lease staleness, plain non-ff rejection, and the server-side
    # race ("cannot lock ref" / "[remote rejected]") when two pushes collide.
    if any(hint in err for hint in
           ("stale info", "[rejected]", "[remote rejected]", "cannot lock ref", "fetch first")):
        return False
    raise Die(f"push to {remote} {full_ref} failed: {err.strip()[:500]}")


def _push_noop(proc: subprocess.CompletedProcess) -> bool:
    return "Everything up-to-date" in ((proc.stderr or "") + (proc.stdout or ""))
