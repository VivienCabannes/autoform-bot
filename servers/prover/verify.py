"""Shared proof-verification gate — the honesty backstop for EVERY prover backend.

An adapter reports ``status="proved"`` from its worker's self-report. That is a
CLAIM, not a proof. Before the driver lets the claim stand, this gate INDEPENDENTLY
verifies the work the worker actually landed — using the Lean **kernel**, because
the cheap checks are unsound:

  * ``lake build`` / ``lake env lean`` **exit 0 on a ``sorry``** (it is only a
    *warning*), so an exit-code build check never catches incompleteness; and
  * a text scan for ``sorry`` can be fooled by string literals / comments and is
    blind to a ``sorry`` reached through an imported file.

So the authoritative check is ``#print axioms`` on the touched declarations: a proof
that rests on a ``sorry`` anywhere in its transitive dependencies reports
``sorryAx`` in its axiom set (verified: a clean ``Main`` importing a ``sorry``'d
``Lemma`` still prints ``'main' depends on axioms: [sorryAx]``). The gate:

  1. **lakefile present** — else (fail-CLOSED for real runs) the proof cannot be
     verified, so a claimed ``proved`` is rejected. (A planner Phase-0 precondition
     guarantees a lakefile in production; tests pass ``require_lakefile=False``.)
  2. **something landed** — ≥1 ``.lean`` changed (uncommitted *and* committed), with
     a node→module fallback.
  3. **build clean** — ``lake build`` exits 0 (catches genuine compile errors; a
     ``sorry`` is only a warning so it is step 4's job, not the build's) and produces
     the ``.olean`` the probe imports.
  4. **no ``sorryAx``** — ``#print axioms`` over the touched modules' declarations
     contains no ``sorryAx`` (catches literal AND transitive/imported gaps).

Any failure → ``ok=False`` and the driver downgrades the verdict to ``failed``. The
deeper non-``sorry`` axiom audit (custom ``axiom``/orphan classes) stays the
proof-integrity reviewer's job. Every ``lake`` call scrubs ``ANTHROPIC_API_KEY``.
All external effects (git, file read, ``lake build``, the ``#print axioms`` probe)
sit behind injectable seams so the gate's logic is unit-testable with no toolchain.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SORRY_RE = re.compile(r"\b(?:sorry|admit|sorryAx)\b(?!-)")
_BLOCK_COMMENT_RE = re.compile(r"/-.*?-/", re.DOTALL)
_MODULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$")
_NS_RE = re.compile(r"^namespace\s+([A-Za-z_][A-Za-z0-9_'.]*)")
_DECL_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(?:theorem|lemma|def|abbrev|instance)\s+([A-Za-z_][A-Za-z0-9_'.]*)"
)
_BUILD_TIMEOUT = 900
_PROBE_TIMEOUT = 300


@dataclass
class VerifyResult:
    """Outcome of the gate. ``checks`` is informational (→ the ProofResult ``meta``
    so a real pass, a skip, and the failing check are all auditable)."""

    ok: bool
    reason: str = ""
    checks: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- utils

def _strip_comments(src: str) -> str:
    src = _BLOCK_COMMENT_RE.sub(" ", src)
    out = []
    for line in src.splitlines():
        i = line.find("--")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def has_sorry(src: str) -> bool:
    """Fast pre-filter: a literal ``sorry``/``admit``/``sorryAx`` in source (outside
    comments). Not authoritative — the kernel check below is — but a cheap early out."""
    return bool(_SORRY_RE.search(_strip_comments(src)))


def _scrubbed_env() -> dict:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def find_lakefile(project_dir: str) -> bool:
    try:
        p = Path(project_dir).resolve()
    except Exception:
        return False
    for d in (p, *p.parents):
        if (d / "lakefile.toml").exists() or (d / "lakefile.lean").exists():
            return True
    return False


# ----------------------------------------------------------------- touched files

@dataclass
class Baseline:
    """Git state of ``project_dir`` at RUN START, for change attribution.

    ``git status`` alone cannot attribute changes to *this* run: a pre-existing
    dirty file would let a run that landed nothing pass the gate on the user's old
    edits (false proved), and a sibling worker's in-progress ``sorry`` would fail
    an unrelated run (false failed). The driver captures a :class:`Baseline`
    before ``adapter.start`` and passes it to :func:`verify_proof` explicitly (no
    global state); "touched by the run" is then *newly* dirty files, files whose
    content hash changed since the baseline, and files committed since the
    baseline ``head``.

    Args:
        dirty_hashes: dirty ``.lean`` path → content hash at capture time.
        head: the ``HEAD`` commit sha at capture time (``""`` if unknown).
        captured: whether git state was successfully read — when ``False``
            (non-git dir, git missing) attribution is impossible and the gate
            falls back to the no-baseline behaviour.
    """

    dirty_hashes: dict[str, str] = field(default_factory=dict)
    head: str = ""
    captured: bool = False


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return "unreadable"


def capture_baseline(project_dir: str) -> Baseline:
    """Snapshot the dirty ``.lean`` files (path → content hash) + ``HEAD`` sha.

    Never raises: on any git failure it returns an un-``captured`` baseline and
    the gate behaves exactly as it did without one.
    """
    try:
        out = subprocess.run(["git", "-C", project_dir, "status", "--porcelain", "-z"],
                             capture_output=True, timeout=30)
        if out.returncode != 0:
            return Baseline()
        dirty = parse_porcelain_z(out.stdout)
        head_p = subprocess.run(["git", "-C", project_dir, "rev-parse", "HEAD"],
                                capture_output=True, timeout=30)
        head = head_p.stdout.decode("ascii", "ignore").strip() if head_p.returncode == 0 else ""
        root = Path(project_dir)
        return Baseline(
            dirty_hashes={f: _hash_file(root / f) for f in dirty},
            head=head,
            captured=True,
        )
    except Exception:
        return Baseline()


def parse_porcelain_z(data: bytes) -> list[str]:
    """Parse ``git status --porcelain -z`` (NUL-separated, no quoting) → changed
    ``.lean`` paths, skipping deletions and consuming the rename/copy origin field."""
    fields = data.split(b"\x00")
    files: list[str] = []
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if len(rec) < 3:
            continue
        xy = rec[:2].decode("ascii", "ignore")
        path = rec[3:].decode("utf-8", "surrogateescape")
        if xy and (xy[0] in "RC" or xy[1:2] in ("R", "C")):
            i += 1  # rename/copy: the next field is the origin path — consume it
        if "D" in xy:          # deletion (staged or worktree) — nothing to verify
            continue
        if path.endswith(".lean"):
            files.append(path)
    return files


def _git_lean_changes(project_dir: str) -> list[str]:
    """Touched ``.lean`` files: uncommitted (``status``) first, else the worker's last
    commit (``diff HEAD~1 HEAD``) — so a worker that committed its proof is covered."""
    try:
        out = subprocess.run(["git", "-C", project_dir, "status", "--porcelain", "-z"],
                             capture_output=True, timeout=30).stdout
        files = parse_porcelain_z(out)
        if files:
            return files
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["git", "-C", project_dir, "diff", "--name-only", "-z", "--diff-filter=d", "HEAD~1", "HEAD"],
            capture_output=True, timeout=30).stdout
        return [p for p in out.decode("utf-8", "surrogateescape").split("\x00") if p.endswith(".lean")]
    except Exception:
        return []


def _node_file_fallback(node: str, project_dir: str) -> list[str]:
    """Module-id node (``A.B``) → ``A/B.lean`` under the project root or ``src/``.
    Rejects non-module ids (prose/kebab) so a stray ``node`` cannot escape the dir."""
    if not _MODULE_ID_RE.match(node or ""):
        return []
    rel = node.replace(".", "/") + ".lean"
    for sub in ("", "src"):
        p = Path(project_dir) / sub / rel
        if p.exists():
            return [str(p.relative_to(project_dir))]
    return []


def _touched_lean(project_dir: str, node: str) -> list[str]:
    return _git_lean_changes(project_dir) or _node_file_fallback(node, project_dir)


def _attributable_lean(project_dir: str, baseline: Baseline) -> list[str]:
    """``.lean`` files attributable to THIS run, judged against ``baseline``:
    newly dirty, dirty with a changed content hash, or committed since the
    baseline ``head``. Pre-existing dirty files whose content is unchanged are
    NOT attributed (they are the user's / a sibling worker's, not this run's)."""
    files: list[str] = []
    try:
        out = subprocess.run(["git", "-C", project_dir, "status", "--porcelain", "-z"],
                             capture_output=True, timeout=30).stdout
        root = Path(project_dir)
        for f in parse_porcelain_z(out):
            prior = baseline.dirty_hashes.get(f)
            if prior is None or _hash_file(root / f) != prior:
                files.append(f)
    except Exception:
        pass
    if baseline.head:
        # The worker may have COMMITTED its proof: diff the baseline head to HEAD.
        try:
            out = subprocess.run(
                ["git", "-C", project_dir, "diff", "--name-only", "-z", "--diff-filter=d",
                 baseline.head, "HEAD"],
                capture_output=True, timeout=30).stdout
            files += [p for p in out.decode("utf-8", "surrogateescape").split("\x00")
                      if p.endswith(".lean") and p not in files]
        except Exception:
            pass
    return files


# ------------------------------------------------------------ module + decl parse

def _module_of(file: str, project_dir: str) -> str | None:
    """``A/B/C.lean`` (relative to the project root) → module name ``A.B.C``."""
    try:
        rel = Path(file)
        if rel.is_absolute():
            rel = rel.relative_to(Path(project_dir).resolve())
    except Exception:
        rel = Path(file)
    s = str(rel)
    if not s.endswith(".lean"):
        return None
    mod = s[:-5].replace(os.sep, ".").replace("/", ".")
    return mod if _MODULE_ID_RE.match(mod) else None


def _decls_of(src: str) -> list[str]:
    """Top-level declaration names (namespace-qualified) the gate will kernel-check."""
    decls: list[str] = []
    stack: list[tuple[str, str]] = []   # (kind, name) for namespace/section, to match `end`
    for raw in _strip_comments(src).splitlines():
        s = raw.strip()
        m = _NS_RE.match(s)
        if m:
            stack.append(("ns", m.group(1)))
            continue
        if re.match(r"^section\b", s):
            stack.append(("sec", ""))
            continue
        if re.match(r"^end\b", s):
            if stack:
                stack.pop()
            continue
        m = _DECL_RE.match(s)
        if m:
            ns = ".".join(e[1] for e in stack if e[0] == "ns" and e[1])
            decls.append(f"{ns}.{m.group(1)}" if ns else m.group(1))
    # de-dup, preserve order
    return list(dict.fromkeys(decls))


def _build_probe(files: list[str], project_dir: str, reader: Callable[[Path], str]) -> tuple[str, list[str], list[str]]:
    """Assemble the ``#print axioms`` probe: ``import <modules>`` + one ``#print
    axioms <decl>`` per touched declaration."""
    modules, decls = [], []
    for f in files:
        mod = _module_of(f, project_dir)
        if mod and mod not in modules:
            modules.append(mod)
        fp = Path(f) if Path(f).is_absolute() else Path(project_dir) / f
        try:
            decls.extend(_decls_of(reader(fp)))
        except Exception:
            continue
    decls = list(dict.fromkeys(decls))
    body = "\n".join(f"import {m}" for m in modules)
    body += "\n" + "\n".join(f"#print axioms {d}" for d in decls)
    return body, modules, decls


# ----------------------------------------------------------------- real runners

def _lake_build(project_dir: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["lake", "build"], cwd=project_dir, env=_scrubbed_env(),
                           capture_output=True, text=True, timeout=_BUILD_TIMEOUT)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"lake build timed out after {_BUILD_TIMEOUT}s"
    except Exception as e:
        return 1, f"lake build: {e}"


def _run_probe(probe_src: str, project_dir: str) -> tuple[int, str]:
    """Run the ``#print axioms`` probe under the project's lake env."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".lean", delete=False) as fh:
            fh.write(probe_src)
            tmp = fh.name
        p = subprocess.run(["lake", "env", "lean", tmp], cwd=project_dir, env=_scrubbed_env(),
                           capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"#print axioms probe timed out after {_PROBE_TIMEOUT}s"
    except Exception as e:
        return 1, f"#print axioms probe: {e}"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ----------------------------------------------------------------- the gate

def verify_proof(
    node: str,
    project_dir: str,
    *,
    baseline: Baseline | None = None,
    touched: list[str] | None = None,
    reader: Callable[[Path], str] | None = None,
    builder: Callable[[str], tuple[int, str]] | None = None,
    prober: Callable[[str, str], tuple[int, str]] | None = None,
    has_lakefile: bool | None = None,
    require_lakefile: bool = True,
) -> VerifyResult:
    """Independently verify a *claimed* proof for ``node`` in ``project_dir``.

    ``baseline`` (captured by the driver at run start via :func:`capture_baseline`
    and threaded through explicitly) scopes "touched" to changes attributable to
    THIS run — without it, a pre-existing dirty file could yield a false "proved"
    and a sibling worker's in-progress ``sorry`` a false "failed".

    Seams — ``touched`` / ``reader`` / ``builder`` (``lake build`` → (rc, out)) /
    ``prober`` (the ``#print axioms`` runner → (rc, out)) / ``has_lakefile`` — are
    injectable for tests; the real defaults use git + the filesystem + ``lake``."""
    has_lake = find_lakefile(project_dir) if has_lakefile is None else has_lakefile
    if not has_lake:
        if require_lakefile:
            logger.warning("verify: no lakefile under %s — cannot verify; rejecting claim", project_dir)
            return VerifyResult(False, "no lakefile reachable — the proof cannot be verified",
                                {"verified": False})
        return VerifyResult(True, "", {"verified": False, "skipped": "no lakefile"})

    if touched is not None:
        files = touched
    elif baseline is not None and baseline.captured:
        # Attribute changes to THIS run against the baseline. When git attribution
        # is authoritative (baseline captured) and nothing is attributable, the
        # claim fails outright — the node→module fallback would re-admit exactly
        # the pre-existing edits the baseline exists to exclude.
        files = _attributable_lean(project_dir, baseline)
        if not files:
            return VerifyResult(
                False,
                "nothing landed — no .lean change is attributable to this run "
                "(pre-existing dirty files are not counted)",
                {"verified": True, "files": [], "attribution": "baseline"})
    else:
        files = _touched_lean(project_dir, node)
    if not files:
        return VerifyResult(False, "no Lean file was written — nothing to verify",
                            {"verified": True, "files": []})

    read = reader or (lambda pth: Path(pth).read_text(encoding="utf-8", errors="ignore"))

    # 1. fast pre-filter — a literal sorry/admit in the touched source.
    for f in files:
        fp = Path(f) if Path(f).is_absolute() else Path(project_dir) / f
        try:
            if has_sorry(read(fp)):
                return VerifyResult(False, f"`{f}` contains a literal sorry/admit",
                                    {"verified": True, "files": files, "sorry_in": f})
        except Exception:
            continue

    # 2. build clean — genuine compile errors only (rc). A `sorry` is just a warning
    #    (rc 0) and is the kernel check's job below; we do NOT scan build output for
    #    sorry warnings because `lake build` is whole-project and would flag OTHER
    #    in-progress files. This step also produces the .olean the probe imports.
    build = builder or _lake_build
    rc, bout = build(project_dir)
    if rc != 0:
        return VerifyResult(False, f"`lake build` failed (exit {rc}): {' '.join(bout.split())[-300:]}",
                            {"verified": True, "files": files, "build": "error"})

    # 3. AUTHORITATIVE kernel check — #print axioms must show no sorryAx (catches a
    #    sorry reached through an imported/untouched file too).
    probe, modules, decls = _build_probe(files, project_dir, read)
    if not decls:
        # no declarations to kernel-check; the build + pre-filter passed.
        return VerifyResult(True, "", {"verified": True, "files": files, "build": "clean", "kernel": "no-decls"})
    prove_probe = prober or _run_probe
    prc, pout = prove_probe(probe, project_dir)
    if "sorryAx" in pout:
        return VerifyResult(False, "proof depends on `sorryAx` — a sorry/admit, possibly via an imported file",
                            {"verified": True, "files": files, "kernel": "sorryAx"})
    if "axioms" not in pout:   # no #print-axioms report ran (import failed / module not built)
        return VerifyResult(False, f"could not kernel-verify (no axiom report; lean exit {prc}): {' '.join(pout.split())[-200:]}",
                            {"verified": True, "files": files, "kernel": "unverified"})
    return VerifyResult(True, "", {"verified": True, "files": files, "build": "clean",
                                   "modules": modules, "decls": len(decls), "kernel": "clean"})
