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

  5. **axiom whitelist** — every axiom the kernel reports must be one of Lean's
     standard axioms (``propext`` / ``Classical.choice`` / ``Quot.sound`` /
     ``funext``) or listed in the project's axiom ledger (``AXIOM_AUDIT.md`` at the
     project root), so an axiom-stubbed "proof" cannot pass the gate.

Any failure → ``ok=False`` and the driver downgrades the verdict to ``failed``. The
deeper axiom audit (orphan classes, whether a ledgered axiom is justified) stays
the proof-integrity reviewer's job. Every ``lake`` call scrubs ``ANTHROPIC_API_KEY``.
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
_UNSAFE_ELAB_RE = re.compile(
    r"\b(?:run_cmd|initialize|elab|foreign|extern|syntax|"
    r"macro|macro_rules|native_decide|run_tac|include_str|include_bytes)\b|"
    r"#(?:eval|reduce|run)\b|"
    r"\bunsafe\s+(?:def|abbrev|theorem|instance)\b"
)
_MODULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$")
_NS_RE = re.compile(r"^namespace\s+([A-Za-z_][A-Za-z0-9_'.]*)")
_DECL_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(?:theorem|lemma|def|abbrev|instance|structure|class|inductive|axiom|opaque)\s+"
    r"([A-Za-z_][A-Za-z0-9_'.]*)"
)
_BUILD_TIMEOUT = 900
_PROBE_TIMEOUT = 900

# The standard axioms a genuine Mathlib-style proof may rest on. Modern Lean 4
# reports the core trio (funext is a theorem, derived from Quot.sound, but is kept
# for older/alternative toolchains that surface it as an axiom). Anything else must
# be whitelisted in the project's axiom ledger or the gate fails.
_STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound", "funext"})

#: Ledger filenames probed (first hit wins) at the project root.
_AXIOM_LEDGER_NAMES = ("AXIOM_AUDIT.md", "AXIOM_AUDIT.txt", "axiom_audit.md", "AXIOMS.md")

_AXIOM_REPORT_RE = re.compile(r"depends on axioms:\s*\[([^\]]*)\]")
_DECL_REPORT_RE = re.compile(r"'([^'\n]+)'\s+(?:does not depend|depends)\b")
_EXPECTED_DECL_OK_RE = re.compile(r"AUTOFORM_EXPECTED_DECL_OK\s+([^\s]+)")
_LEDGER_AXIOM_RE = re.compile(r"\baxiom\s+`?([A-Za-z_][A-Za-z0-9_.']*)`?")

#: Sentinel the enumeration probe prints once it has scanned EVERY declaration the
#: touched modules own. Its absence means the probe died mid-scan → fail closed.
_PROBE_DONE = "AUTOFORM_PROBE_OK"
_PROBE_DONE_RE = re.compile(r"AUTOFORM_PROBE_OK decls=(\d+)")


@dataclass
class VerifyResult:
    """Outcome of the gate. ``checks`` is informational (→ the ProofResult ``meta``
    so a real pass, a skip, and the failing check are all auditable)."""

    ok: bool
    reason: str = ""
    checks: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- utils

def _strip_comments(src: str) -> str:
    """Remove Lean line/nested-block comments while preserving quoted strings.

    Preserving strings is deliberately conservative for the unsafe-code scan:
    a keyword in a string can cause a false rejection, but comment delimiters
    embedded in a string cannot be used to hide a later executable directive.
    """
    out: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(src):
        pair = src[index : index + 2]
        char = src[index]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                out.extend("  ")
                index += 2
            elif pair == "-/":
                block_depth -= 1
                out.extend("  ")
                index += 2
            else:
                out.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if pair == "--":
            while index < len(src) and src[index] != "\n":
                out.append(" ")
                index += 1
            continue
        if pair == "/-":
            block_depth = 1
            out.extend("  ")
            index += 2
            continue
        out.append(char)
        if char == '"':
            in_string = True
        index += 1
    return "".join(out)


def has_sorry(src: str) -> bool:
    """Fast pre-filter: a literal ``sorry``/``admit``/``sorryAx`` in source (outside
    comments). Not authoritative — the kernel check below is — but a cheap early out."""
    return bool(_SORRY_RE.search(_strip_comments(src)))


def unsafe_elaboration_directive(src: str) -> str:
    """Return a generated-code execution directive that must not reach ``lean``.

    Lean elaboration is programmable. A provider-written file containing
    ``run_cmd``, ``initialize``, custom elaborators/macros, native execution,
    foreign declarations, or compile-time file inclusion can run code/read data
    during the verification build, before the kernel/axiom audit gets a vote.
    Autoform proof workers do not need these forms, so the gate fails closed on
    touched files containing one.
    """
    match = _UNSAFE_ELAB_RE.search(_strip_comments(src))
    return match.group(0).strip() if match else ""


def _scrubbed_env() -> dict:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
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
        out = subprocess.run(["git", "-C", project_dir, "status", "--porcelain", "-z", "--untracked-files=all"],
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
        out = subprocess.run(["git", "-C", project_dir, "status", "--porcelain", "-z", "--untracked-files=all"],
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
        out = subprocess.run(["git", "-C", project_dir, "status", "--porcelain", "-z", "--untracked-files=all"],
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


# ------------------------------------------------------------------ axiom audit

def parse_axiom_ledger(text: str) -> set[str]:
    """Axiom names a project ledger (AXIOM_AUDIT.md) explicitly allows.

    Deliberately FORGIVING about format: it accepts inline/fenced ``axiom <name>``
    declarations anywhere in the text, and bullet-list items that are a single
    axiom name (``- Foo.bar`` / ``* Foo.bar``, optionally backticked). Arbitrary
    prose words are NOT picked up — only ``axiom``-prefixed names and list items."""
    names = set(_LEDGER_AXIOM_RE.findall(text))
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] not in "-*+":
            continue
        s = s.lstrip("-*+ \t").strip().strip("`")
        if s and _MODULE_ID_RE.match(s):
            names.add(s)
    return names


def _ledger_axioms(project_dir: str) -> set[str]:
    """Read the project's axiom ledger, if one exists at the project root."""
    for name in _AXIOM_LEDGER_NAMES:
        p = Path(project_dir) / name
        try:
            if p.exists():
                return parse_axiom_ledger(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:  # pragma: no cover - unreadable ledger = no allowances
            continue
    return set()


def _axioms_in_report(pout: str) -> set[str]:
    """Every axiom named in ``#print axioms`` output (`'d' depends on axioms: [...]`)."""
    names: set[str] = set()
    for m in _AXIOM_REPORT_RE.finditer(pout):
        names.update(a.strip() for a in m.group(1).split(",") if a.strip())
    return names


def _decls_probed(pout: str) -> int:
    """How many module-owned declarations the enumeration probe reported (0 if the
    sentinel is absent)."""
    m = _PROBE_DONE_RE.search(pout)
    return int(m.group(1)) if m else 0


def _declarations_in_report(pout: str) -> set[str]:
    """Declaration names whose axiom report was emitted by the kernel probe."""
    marked = {match.group(1) for match in _EXPECTED_DECL_OK_RE.finditer(pout)}
    return marked or {match.group(1) for match in _DECL_REPORT_RE.finditer(pout)}


def _project_relative(path: str, project_dir: str) -> str | None:
    """Normalize a project-contained path for target-attribution comparisons."""
    root = Path(project_dir).resolve()
    candidate = Path(path)
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return None


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


def _build_probe(
    files: list[str],
    project_dir: str,
    expected_declarations: list[str] | None = None,
    *,
    compat_axioms: bool = False,
) -> tuple[str, list[str]]:
    """Assemble an AUTHORITATIVE ``#print axioms`` probe.

    Rather than scraping declaration names from source with a regex (which cannot see
    anonymous ``instance``s, ``alias``/``structure``/``axiom``, macro-generated decls,
    or multiline signatures — any of which can carry a transitive ``sorryAx`` past the
    gate), this imports the touched modules and enumerates every declaration those
    modules OWN directly from the compiled Lean **environment** (the build artifact),
    printing each one's axiom set in the same ``depends on axioms: [...]`` form the
    downstream parser already understands. A final ``AUTOFORM_PROBE_OK decls=N``
    sentinel proves the scan ran to completion, so a probe that dies mid-enumeration
    fails closed instead of silently skipping the un-scanned (possibly tainted) decls.

    ``_decls_of`` (the old source regex) is kept only for the fast literal-sorry
    pre-filter's siblings and tests; it is no longer the authority for the gate."""
    modules: list[str] = []
    for f in files:
        mod = _module_of(f, project_dir)
        if mod and mod not in modules:
            modules.append(mod)
    declarations = list(expected_declarations or [])
    if not modules or not all(_MODULE_ID_RE.match(name) for name in declarations):
        return "", []
    imports = "\n".join(f"import {m}" for m in modules)
    mod_lits = ", ".join(f"`{m}" for m in modules)
    decl_lits = ", ".join(f"`{name}" for name in declarations)
    ownership_check = ""
    if declarations:
        ownership_check = (
            f"  let expectedDecls : List Name := [{decl_lits}]\n"
            "  for decl in expectedDecls do\n"
            "    match env.getModuleIdxFor? decl with\n"
            "    | some actual =>\n"
            "        unless idxs.contains actual do\n"
            "          throwError m!\"wrong owner for {decl}: expected one of {mods}\"\n"
            "    | none => throwError m!\"missing declaration {decl}\"\n"
            "    logInfo m!\"AUTOFORM_EXPECTED_DECL_OK {decl}\"\n"
        )
    compat_support = ""
    axiom_call = "        let axs ← collectAxioms nm\n"
    if compat_axioms:
        compat_support = (
        "namespace AutoformVerify\n"
        "\n"
        "open Lean\n"
        "\n"
        "structure AxiomState where\n"
        "  visited : NameSet := {}\n"
        "  axioms : Array Name := #[]\n"
        "\n"
        "abbrev AxiomM := ReaderT Environment (StateM AxiomState)\n"
        "\n"
        "partial def collectAxiomsCompat (c : Name) : AxiomM Unit := do\n"
        "  let collectExpr (e : Expr) : AxiomM Unit :=\n"
        "    e.getUsedConstants.forM collectAxiomsCompat\n"
        "  let state ← get\n"
        "  unless state.visited.contains c do\n"
        "    modify fun s => { s with visited := s.visited.insert c }\n"
        "    let env ← read\n"
        "    match env.find? c with\n"
        "    | some (ConstantInfo.axiomInfo _) =>\n"
        "        modify fun s => { s with axioms := s.axioms.push c }\n"
        "    | some (ConstantInfo.defnInfo v) => collectExpr v.type *> collectExpr v.value\n"
        "    | some (ConstantInfo.thmInfo v) => collectExpr v.type *> collectExpr v.value\n"
        "    | some (ConstantInfo.opaqueInfo v) => collectExpr v.type *> collectExpr v.value\n"
        "    | some (ConstantInfo.quotInfo _) => pure ()\n"
        "    | some (ConstantInfo.ctorInfo v) => collectExpr v.type\n"
        "    | some (ConstantInfo.recInfo v) => collectExpr v.type\n"
        "    | some (ConstantInfo.inductInfo v) => collectExpr v.type *> v.ctors.forM collectAxiomsCompat\n"
        "    | none => pure ()\n"
        "\n"
        "def axiomsOf (env : Environment) (nm : Name) : Array Name :=\n"
        "  let (_, state) := ((collectAxiomsCompat nm).run env).run {}\n"
        "  state.axioms\n"
        "\n"
        "end AutoformVerify\n"
        "\n"
        )
        axiom_call = "        let axs := AutoformVerify.axiomsOf env nm\n"
    probe = (
        "import Lean\n"
        f"{imports}\n"
        f"{compat_support}"
        "open Lean in\n"
        "run_cmd do\n"
        "  let env ← getEnv\n"
        f"  let mods : List Name := [{mod_lits}]\n"
        "  let idxs := mods.filterMap env.getModuleIdx?\n"
        "  let mut n : Nat := 0\n"
        "  for (nm, _ci) in env.constants.toList do\n"
        "    if let some i := env.getModuleIdxFor? nm then\n"
        "      if idxs.contains i && !nm.isInternalDetail then\n"
        "        n := n + 1\n"
        f"{axiom_call}"
        "        if axs.isEmpty then\n"
        "          logInfo m!\"'{nm}' does not depend on any axioms\"\n"
        "        else\n"
        "          logInfo m!\"'{nm}' depends on axioms: {axs.toList}\"\n"
        f"{ownership_check}"
        f"  logInfo m!\"{_PROBE_DONE} decls={{n}}\"\n"
    )
    return probe, modules


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
    expected_files: list[str] | None = None,
    expected_declarations: list[str] | None = None,
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

    expected_normalized: set[str] = set()
    if expected_files:
        actual = {_project_relative(file, project_dir) for file in files}
        for expected in expected_files:
            normalized = _project_relative(expected, project_dir)
            if normalized is None:
                return VerifyResult(
                    False,
                    f"expected target file escapes the project: {expected}",
                    {"verified": True, "files": files, "target_file": "unsafe"},
                )
            if normalized not in actual:
                return VerifyResult(
                    False,
                    f"expected target file was not changed by this run: {normalized}",
                    {"verified": True, "files": files, "target_file": "untouched",
                     "expected_files": list(expected_files)},
                )
            expected_normalized.add(normalized)

    read = reader or (lambda pth: Path(pth).read_text(encoding="utf-8", errors="ignore"))

    # 1. fast pre-filters — literal proof gaps and elaboration-time execution.
    for f in files:
        fp = Path(f) if Path(f).is_absolute() else Path(project_dir) / f
        try:
            source = read(fp)
            if has_sorry(source):
                return VerifyResult(False, f"`{f}` contains a literal sorry/admit",
                                    {"verified": True, "files": files, "sorry_in": f})
            unsafe = unsafe_elaboration_directive(source)
            if unsafe:
                return VerifyResult(
                    False,
                    f"`{f}` contains disallowed elaboration-time execution: {unsafe}",
                    {"verified": True, "files": files, "unsafe_elaboration_in": f},
                )
            normalized = _project_relative(f, project_dir)
            if expected_normalized and normalized not in expected_normalized and _decls_of(source):
                return VerifyResult(
                    False,
                    f"off-target file `{f}` adds declarations; expected target file(s): "
                    + ", ".join(sorted(expected_normalized)),
                    {"verified": True, "files": files, "kernel": "off-target-declarations",
                     "expected_files": sorted(expected_normalized)},
                )
        except Exception as error:
            return VerifyResult(
                False,
                f"could not read attributable Lean file `{f}`: {error}",
                {"verified": True, "files": files, "source_read": "error"},
            )

    # 2. build clean — genuine compile errors only (rc). A `sorry` is just a warning
    #    (rc 0) and is the kernel check's job below; we do NOT scan build output for
    #    sorry warnings because `lake build` is whole-project and would flag OTHER
    #    in-progress files. This step also produces the .olean the probe imports.
    build = builder or _lake_build
    rc, bout = build(project_dir)
    if rc != 0:
        return VerifyResult(False, f"`lake build` failed (exit {rc}): {' '.join(bout.split())[-300:]}",
                            {"verified": True, "files": files, "build": "error"})

    # 3. AUTHORITATIVE kernel check — enumerate the touched modules' declarations from
    #    the compiled environment and #print their axioms; must show no sorryAx (catches
    #    a sorry reached through an imported/untouched file, an anonymous instance, or
    #    any decl form a source scan would miss). The sentinel guarantees completeness.
    probe_files = sorted(expected_normalized) if expected_normalized else files
    probe, modules = _build_probe(
        probe_files,
        project_dir,
        expected_declarations=list(expected_declarations or []),
    )
    if not modules:
        return VerifyResult(False, "could not resolve a Lean module from the touched files — cannot kernel-verify",
                            {"verified": True, "files": files, "kernel": "no-modules"})
    prove_probe = prober or _run_probe
    prc, pout = prove_probe(probe, project_dir)
    if _PROBE_DONE not in pout and "collectAxioms" in pout:
        # Lean 4.9 used a different ``collectAxioms`` API. Keep one compatibility
        # retry for older projects; modern Lean uses its cached module summaries.
        compat_probe, _ = _build_probe(
            probe_files,
            project_dir,
            expected_declarations=list(expected_declarations or []),
            compat_axioms=True,
        )
        prc, pout = prove_probe(compat_probe, project_dir)
    if "sorryAx" in pout:
        return VerifyResult(False, "proof depends on `sorryAx` — a sorry/admit, possibly via an imported file",
                            {"verified": True, "files": files, "kernel": "sorryAx"})
    if _PROBE_DONE not in pout:   # enumeration did not finish (import failed / not built / crash) → fail closed
        return VerifyResult(False, f"could not kernel-verify (axiom enumeration did not complete; lean exit {prc}): {' '.join(pout.split())[-200:]}",
                            {"verified": True, "files": files, "kernel": "unverified"})

    # 4. axiom WHITELIST — sorryAx is not the only way to fake a proof: an
    #    axiom-stubbed lemma passes the check above. Only Lean's standard axioms
    #    (plus any the project's axiom ledger explicitly audits) may appear.
    axioms = _axioms_in_report(pout)
    allowed = _STANDARD_AXIOMS | _ledger_axioms(project_dir)
    rogue = sorted(axioms - allowed)
    if rogue:
        return VerifyResult(
            False,
            f"proof depends on non-standard axiom(s) not in the project ledger: {', '.join(rogue)}",
            {"verified": True, "files": files, "kernel": "axiom",
             "axioms": sorted(axioms), "rogue_axioms": rogue})
    ndecls = _decls_probed(pout)
    if ndecls == 0:
        return VerifyResult(
            False,
            "touched modules own no declarations — no formalization target was verified",
            {"verified": True, "files": files, "build": "clean",
             "modules": modules, "decls": 0, "kernel": "no-decls",
             "axioms": sorted(axioms)},
        )
    if expected_declarations:
        reported = _declarations_in_report(pout)
        missing = [name for name in expected_declarations if name not in reported]
        if missing:
            return VerifyResult(
                False,
                "expected target declaration(s) were not found in the touched modules: "
                + ", ".join(missing),
                {"verified": True, "files": files, "build": "clean",
                 "modules": modules, "decls": ndecls, "kernel": "missing-declaration",
                 "expected_declarations": list(expected_declarations),
                 "reported_declarations": sorted(reported),
                 "axioms": sorted(axioms)},
            )
    return VerifyResult(True, "", {"verified": True, "files": files, "build": "clean",
                                   "modules": modules, "decls": ndecls,
                                   "kernel": "clean",
                                   "axioms": sorted(axioms)})
