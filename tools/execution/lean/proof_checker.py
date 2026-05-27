# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Proof checker — validates Lean proofs using the REPL.

Provides `LeanProofChecker` which uses a `LeanRepl` to run code snippets
sequentially and optionally verify axiom usage.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .repl import LeanRepl, format_message
from .constant import STANDARD_AXIOMS

logger = getLogger(__name__)


@dataclass(frozen=True)
class ProofCheckResult:
    """Result of a proof check."""

    valid: bool
    reason: str
    exit_code: int = 0
    compiler_output: str = ""
    execution_time: float = 0.0


def parse_axioms_from_output(stdout: str) -> set[str]:
    """Parse axiom names from `#print axioms` output.

    Lean outputs lines like:
        'foo' depends on axioms: [propext, Classical.choice, Quot.sound]
    or for no axioms:
        'foo' does not depend on any axioms

    Returns the set of all axiom names found.
    """
    axioms: set[str] = set()
    for m in re.finditer(r"depends on axioms:\s*\[([^\]]*)\]", stdout):
        for name in m.group(1).split(","):
            name = name.strip()
            if name:
                axioms.add(name)
    return axioms


def parse_axioms_per_decl(output: str) -> dict[str, set[str]]:
    """Parse per-declaration axiom dependencies from ``#print axioms`` output.

    Returns a mapping from qualified declaration name to its axiom set.
    Declarations with no axioms are included with an empty set.
    """
    result: dict[str, set[str]] = {}
    for m in re.finditer(r"'(.+?)' depends on axioms:\s*\[([^\]]*)\]", output):
        decl = m.group(1)
        axioms = {a.strip() for a in m.group(2).split(",") if a.strip()}
        result[decl] = axioms
    for m in re.finditer(r"'(.+?)' does not depend on any axioms", output):
        result.setdefault(m.group(1), set())
    return result


def _format_messages(messages: list[dict[str, Any]]) -> str:
    """Format a list of REPL message dicts into a newline-joined string."""
    return "\n".join(format_message(m) for m in messages if isinstance(m, dict))


class LeanProofChecker:
    """Validates Lean proofs using a REPL.

    Requires a started (warm) ``LeanRepl`` — the checker does not own
    or manage the REPL lifecycle.  Runs code snippets sequentially
    (chaining env_id) and optionally verifies axiom usage via
    ``#print axioms``.
    """

    def __init__(
        self,
        repl: LeanRepl,
        check_axioms: bool = True,
        allowed_axioms: set[str] | None = None,
    ) -> None:
        self._repl = repl
        self.check_axioms = check_axioms
        self.allowed_axioms = allowed_axioms or STANDARD_AXIOMS

    def check_snippets(
        self,
        snippets: list[str],
        timeout: float | None = None,
    ) -> ProofCheckResult:
        """Run code snippets sequentially through the REPL.

        Uses ``run_steps()`` to chain environments across snippets, then
        (if ``check_axioms``) parses ``#print axioms`` output for
        non-standard axioms.

        Args:
            snippets: List of Lean code snippets to run in sequence.
                Empty strings are skipped.
            timeout: Timeout per snippet in seconds.

        Returns:
            ProofCheckResult with validity status and details.
        """
        start_time = time.monotonic()

        try:
            results = self._repl.run_steps(snippets, timeout=timeout)
            elapsed = time.monotonic() - start_time

            if not results:
                return ProofCheckResult(
                    valid=True,
                    reason="No snippets to check",
                    exit_code=0,
                    execution_time=elapsed,
                )

            # Collect all messages and check for errors
            all_messages: list[dict[str, Any]] = []
            for result in results:
                # REPL-level error (process died, timeout, etc.)
                if "repl_error" in result:
                    return ProofCheckResult(
                        valid=False,
                        reason=f"REPL error: {result['repl_error']}",
                        exit_code=1,
                        compiler_output=str(result),
                        execution_time=elapsed,
                    )

                messages = result.get("messages", [])
                all_messages.extend(messages)

                # Compilation error (run_steps short-circuits, so the last
                # result may contain errors)
                has_errors = any(isinstance(msg, dict) and msg.get("severity") == "error" for msg in messages)
                if has_errors:
                    return ProofCheckResult(
                        valid=False,
                        reason="Compilation error",
                        exit_code=1,
                        compiler_output=_format_messages(messages),
                        execution_time=elapsed,
                    )

            # Check axioms (last snippet is expected to be #print axioms)
            if self.check_axioms and all_messages:
                combined_output = _format_messages(all_messages)
                axioms = parse_axioms_from_output(combined_output)
                disallowed = axioms - self.allowed_axioms
                if disallowed:
                    return ProofCheckResult(
                        valid=False,
                        reason=f"Disallowed axioms: {', '.join(sorted(disallowed))}",
                        exit_code=0,
                        compiler_output=combined_output,
                        execution_time=elapsed,
                    )

            return ProofCheckResult(
                valid=True,
                reason="Proof is valid",
                exit_code=0,
                compiler_output=_format_messages(all_messages),
                execution_time=elapsed,
            )

        except Exception as e:
            return ProofCheckResult(
                valid=False,
                reason=f"Execution failed: {e}",
                exit_code=1,
                compiler_output=str(e),
                execution_time=time.monotonic() - start_time,
            )
