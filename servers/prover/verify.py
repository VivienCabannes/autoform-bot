"""Shared proof-verification gate — the honesty backstop for EVERY prover backend.

An adapter reports ``status="proved"`` from its worker's self-report. That is a
CLAIM, not a proof. Before the driver lets the claim stand, this gate INDEPENDENTLY
verifies the work the worker actually landed:

  1. **Something landed** — the worker touched at least one ``.lean`` file.
  2. **No gap** — none of the touched files contains a real ``sorry`` / ``admit`` /
     ``sorryAx`` (outside comments).
  3. **Build clean** — each touched file elaborates under ``lake env lean`` (exit 0).

Any failure makes the gate return ``ok=False``; the driver then downgrades the
verdict to ``failed`` with the gate's reason, OVERRIDING the worker's prose — so a
confident message over a sorry'd or non-compiling file can never be reported proved.

This is the *producer-side* gate. The kernel-level ``#print axioms`` audit (sorryAx
introduced via non-literal paths, unexpected/unledgered axioms) stays the
proof-integrity reviewer's job — defense in depth, not duplicated here.

Every ``lake`` invocation scrubs ``ANTHROPIC_API_KEY`` (Max billing hygiene). All
external effects (git, file reads, ``lake``) sit behind injectable seams so the gate
is unit-testable with no Lean toolchain.

It **auto-skips** (passes, logged + flagged in ``checks``) when no lakefile is
reachable — there is nothing to build. In production a lakefile is guaranteed by the
planner's Phase-0 precondition before any worker runs, so the skip only affects
non-Lean test/scratch contexts; it is recorded as ``checks={"verified": False}`` so a
caller can tell a real pass from a skip.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Incompleteness markers (mirrors the dashboard's sorry scanner). Whole-word so it
# never fires inside an identifier (`sorryHandler`); the ``(?!-)`` tail rejects the
# hyphenated prose form (``sorry-free``).
_SORRY_RE = re.compile(r"\b(?:sorry|admit|sorryAx)\b(?!-)")
_BLOCK_COMMENT_RE = re.compile(r"/-.*?-/", re.DOTALL)

_BUILD_TIMEOUT = 600


@dataclass
class VerifyResult:
    """Outcome of the verification gate. ``checks`` is informational (goes to the
    ProofResult ``meta`` so a pass-vs-skip and the failing check are auditable)."""

    ok: bool
    reason: str = ""
    checks: dict = field(default_factory=dict)


def _strip_comments(src: str) -> str:
    """Drop ``/- … -/`` block comments and ``-- …`` line comments before scanning."""
    src = _BLOCK_COMMENT_RE.sub(" ", src)
    out = []
    for line in src.splitlines():
        i = line.find("--")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def has_sorry(src: str) -> bool:
    """Does this Lean source contain a real ``sorry`` / ``admit`` / ``sorryAx``?"""
    return bool(_SORRY_RE.search(_strip_comments(src)))


def _scrubbed_env() -> dict:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def find_lakefile(project_dir: str) -> bool:
    """Is a ``lakefile.toml`` / ``lakefile.lean`` reachable from ``project_dir`` up?"""
    try:
        p = Path(project_dir).resolve()
    except Exception:
        return False
    for d in (p, *p.parents):
        if (d / "lakefile.toml").exists() or (d / "lakefile.lean").exists():
            return True
    return False


def git_touched_lean(project_dir: str) -> list[str]:
    """The ``.lean`` files the worker just changed, from ``git status --porcelain``.

    Backend- and graph-agnostic — it reads the actual edits in the repo. Returns
    repo-relative paths; empty if not a git repo or nothing changed."""
    try:
        out = subprocess.run(
            ["git", "-C", project_dir, "status", "--porcelain"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return []
    files: list[str] = []
    for line in out.splitlines():
        path = line[3:].strip()                 # porcelain: "XY <path>"
        if " -> " in path:                       # rename: "<old> -> <new>"
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path.endswith(".lean"):
            files.append(path)
    return files


def _node_file_fallback(node: str, project_dir: str) -> list[str]:
    """If git shows nothing (e.g. the worker committed its edits), fall back to the
    node's module path: ``A.B.C`` → ``A/B/C.lean`` under the project root or ``src/``.
    Best-effort and only for module-style node ids; empty otherwise."""
    rel = node.replace(".", "/") + ".lean"
    for sub in ("", "src"):
        p = Path(project_dir) / sub / rel
        if p.exists():
            return [str(p.relative_to(project_dir))]
    return []


def _lake_build_file(file: str, project_dir: str) -> tuple[int, str]:
    """Elaborate one file under the project toolchain: ``lake env lean <file>``."""
    try:
        p = subprocess.run(
            ["lake", "env", "lean", file],
            cwd=project_dir, env=_scrubbed_env(),
            capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
        )
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"lake env lean {file}: timed out after {_BUILD_TIMEOUT}s"
    except Exception as e:                        # toolchain missing / not runnable
        return 1, f"lake env lean {file}: {e}"


def verify_proof(
    node: str,
    project_dir: str,
    *,
    touched: list[str] | None = None,
    reader: Callable[[Path], str] | None = None,
    builder: Callable[[str, str], tuple[int, str]] | None = None,
    has_lakefile: bool | None = None,
) -> VerifyResult:
    """Independently verify a *claimed* proof for ``node`` in ``project_dir``.

    Seams — ``touched`` (file list), ``reader`` (file → text), ``builder``
    (file, dir → (rc, output)), ``has_lakefile`` — are injectable for tests; the
    real defaults use git + the filesystem + ``lake env lean``.
    """
    has_lake = find_lakefile(project_dir) if has_lakefile is None else has_lakefile
    if not has_lake:
        logger.warning("verify: no lakefile under %s — skipping build gate (claim unverified)", project_dir)
        return VerifyResult(True, "", {"verified": False, "skipped": "no lakefile"})

    files = (touched if touched is not None
             else git_touched_lean(project_dir) or _node_file_fallback(node, project_dir))
    if not files:
        return VerifyResult(False, "no Lean file was written — nothing to verify",
                            {"verified": True, "files": []})

    read = reader or (lambda pth: Path(pth).read_text(encoding="utf-8", errors="ignore"))

    # 1. no-gap scan on each touched file
    for f in files:
        fp = Path(f) if Path(f).is_absolute() else Path(project_dir) / f
        try:
            src = read(fp)
        except Exception:
            src = ""
        if has_sorry(src):
            return VerifyResult(False, f"`{f}` still contains a sorry/admit/sorryAx",
                                {"verified": True, "files": files, "sorry_in": f})

    # 2. build-clean: each touched file must elaborate
    build = builder or _lake_build_file
    for f in files:
        rc, out = build(f, project_dir)
        if rc != 0:
            tail = " ".join(out.split())[-300:]
            return VerifyResult(False, f"`{f}` does not compile (lake env lean exit {rc}): {tail}",
                                {"verified": True, "files": files, "build_fail": f})

    return VerifyResult(True, "", {"verified": True, "files": files, "build": "clean", "sorry": "none"})
