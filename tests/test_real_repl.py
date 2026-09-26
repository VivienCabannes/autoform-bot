"""Opt-in integration test against the pinned upstream Lean REPL."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from servers.repl.core import LeanRepl, LeanReplConfig


REPL_FIXTURE = Path(__file__).parent / "fixtures" / "repl-smoke"


@pytest.mark.skipif(
    os.environ.get("AUTOFORM_RUN_REAL_REPL_TESTS") != "1",
    reason="set AUTOFORM_RUN_REAL_REPL_TESTS=1 to run the pinned REPL integration",
)
def test_disposable_call_matches_the_pinned_repl_protocol():
    repl = LeanRepl(
        LeanReplConfig(
            cwd=str(REPL_FIXTURE),
            repl_command=["lake", "exe", "repl"],
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )

    response = repl.run_disposable(
        "theorem autoform_repl_probe : True := by sorry",
        timeout=180,
    )

    assert response.get("sorries")
    assert "env" not in response
    assert all("proofState" not in sorry for sorry in response["sorries"])
    assert repl.is_clean()
