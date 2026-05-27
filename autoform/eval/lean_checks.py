# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Programmatic checks for autoformalization eval.

Repo-level checks (compilation, forbidden keywords) gate the entire eval.
Statement-level axiom checks use ``lake env lean`` on a temporary file
with ``#print axioms`` commands.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from logging import getLogger
from pathlib import Path

from tools.execution.lean.constant import FORBIDDEN_KEYWORDS, STANDARD_AXIOMS
from tools.execution.lean.parsing import _iter_code_lines
from tools.execution.lean.proof_checker import parse_axioms_per_decl

logger = getLogger(__name__)

# Files excluded from forbidden keyword scanning (contain legitimate macro/syntax usage)
_FORBIDDEN_KEYWORD_IGNORED_FILES: frozenset[str] = frozenset({"Unproved.lean"})

COMPILE_TIMEOUT = 3600.0


class AxiomCheckError(Exception):
    """Raised for fatal axiom checker failures (lean not found, timeout, missing .olean)."""


class DeclarationNotFoundError(AxiomCheckError):
    """Raised when the declaration name could not be resolved by ``#print axioms``.

    This is recoverable — the matcher agent can retry with a corrected name.
    """


# ---------------------------------------------------------------------------
# Repo-level checks
# ---------------------------------------------------------------------------


class CompilationChecker:
    """Builds the Lean project with ``lake build``.

    Returns whether the build succeeded and the combined stdout/stderr.
    Fails gracefully on timeout or missing ``lake`` binary.
    """

    def __init__(self, repo_dir: Path, *, target: str | None = None, timeout: float = COMPILE_TIMEOUT) -> None:
        self._repo_dir = repo_dir
        self._target = target
        self._timeout = timeout

    async def check(self) -> tuple[bool, str]:
        """Run ``lake build [target]``. Return ``(compiled_ok, build_output)``."""
        cmd = ["lake", "build"]
        if self._target:
            cmd.append(self._target)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return False, f"Compilation timed out after {self._timeout}s"
            output = (stdout.decode() + "\n" + stderr.decode()).strip()
            return proc.returncode == 0, output
        except FileNotFoundError as e:
            return False, f"lake not found — is Lean installed? ({e})"


class ForbiddenKeywordChecker:
    """Detects Lean metaprogramming keywords (``macro``, ``syntax``, ``elab``, etc.).

    These keywords let agents modify the Lean compiler itself, which could
    bypass proof checking. Scans all ``.lean`` files outside ``.lake/``,
    stripping comments to avoid false positives.
    """

    def __init__(self, repo_dir: Path) -> None:
        self._repo_dir = repo_dir

    def check(self) -> list[tuple[str, str]]:
        """Return ``[(relative_file_path, keyword), ...]`` for each violation."""
        violations: list[tuple[str, str]] = []
        for lean_file in self._repo_dir.rglob("*.lean"):
            try:
                rel = lean_file.relative_to(self._repo_dir)
            except ValueError:
                continue
            if ".lake" in rel.parts:
                continue
            if lean_file.name in _FORBIDDEN_KEYWORD_IGNORED_FILES:
                continue

            try:
                code = lean_file.read_text(encoding="utf-8")
            except OSError:
                logger.warning("Could not read %s", lean_file)
                continue

            code_lines = code.splitlines()
            code_without_comments = "\n".join(s for _, s in _iter_code_lines(code_lines))
            for kw in FORBIDDEN_KEYWORDS:
                if re.search(rf"\b{re.escape(kw)}\b", code_without_comments):
                    violations.append((str(rel), kw))

        return violations


# ---------------------------------------------------------------------------
# Statement-level checks (batched at repo level)
# ---------------------------------------------------------------------------


def _lean_file_to_import(lean_file: str) -> str:
    """Convert a relative ``.lean`` path to a Lean import path.

    ``AlgebraicCombinatorics/CauchyBinet.lean`` → ``AlgebraicCombinatorics.CauchyBinet``
    """
    return lean_file.removesuffix(".lean").replace("/", ".")


class AxiomsChecker:
    """Detects non-standard axiom dependencies (e.g. ``sorryAx``).

    Lean proofs may only depend on ``propext``, ``Classical.choice``, and
    ``Quot.sound``.  Anything else (most commonly ``sorryAx`` from an
    incomplete proof) means the declaration is not fully proven.

    Creates a temporary ``.lean`` file with the required imports and
    ``#print axioms`` commands, then runs it via ``lake env lean``.
    """

    def __init__(
        self,
        repo_dir: Path,
        timeout: float = COMPILE_TIMEOUT,
        allowed_axioms: frozenset[str] = frozenset(),
    ) -> None:
        self._repo_dir = repo_dir
        self._timeout = timeout
        self._allowed = STANDARD_AXIOMS | allowed_axioms

    async def check(
        self, decl_names: list[str], lean_files: list[str] | None = None
    ) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
        """Return ``(all_axioms, violations)`` per declaration.

        Raises:
            AxiomCheckError: If any declaration name cannot be resolved,
                lake/lean is not found, or the check times out.
        """
        if not decl_names:
            raise AxiomCheckError("No declaration names provided")

        # Build import set
        if lean_files:
            imports = sorted({_lean_file_to_import(f) for f in lean_files})
        else:
            imports = sorted({".".join(n.split(".")[:-1]) for n in decl_names if "." in n})

        # Create temp file with imports + #print axioms
        lines = [f"import {mod}" for mod in imports]
        lines.append("")
        lines.extend(f"#print axioms {name}" for name in decl_names)
        lines.append("")

        tmp_file = self._repo_dir / f"_axiom_check_{uuid.uuid4().hex[:8]}.lean"
        tmp_file.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Axiom check: decl_names=%s, lean_files=%s", decl_names, lean_files)

        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "lake",
                    "env",
                    "lean",
                    str(tmp_file),
                    cwd=self._repo_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                raise AxiomCheckError("lake/lean binary not found — is Lean installed?")

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise AxiomCheckError(f"Axiom check timed out after {self._timeout}s for {decl_names}")

            output = (stdout.decode() + "\n" + stderr.decode()).strip()
            axioms_by_decl = parse_axioms_per_decl(output)
            logger.info("Axiom check raw parse: %s", {k: sorted(v) for k, v in axioms_by_decl.items()})

            # Detect unknown constants
            unknown = re.findall(r"Unknown constant [`']([^`']+)[`']", output)

            all_axioms: dict[str, frozenset[str]] = {}
            violations: dict[str, frozenset[str]] = {}
            not_found: list[str] = []
            for name in decl_names:
                if name in unknown:
                    not_found.append(f"Unknown constant '{name}'")
                elif name not in axioms_by_decl:
                    not_found.append(f"No axiom output for '{name}' — declaration may not exist or .olean missing")
                else:
                    axioms = axioms_by_decl[name]
                    all_axioms[name] = frozenset(axioms)
                    disallowed = axioms - self._allowed
                    if disallowed:
                        violations[name] = frozenset(disallowed)

            if not_found:
                raise DeclarationNotFoundError("; ".join(not_found))

            return all_axioms, violations
        finally:
            tmp_file.unlink(missing_ok=True)
