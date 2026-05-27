#!/usr/bin/env python3

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Run programmatic rubrics against a workspace and print a report.

Usage:
    python autoform/eval/mini_app.py [workspace_dir] [task_file]

Defaults to runs/example/workspace and runs/example/targets.yaml.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.constants import REPO_ROOT  # noqa: E402
from autoform.eval.lean_checks import (  # noqa: E402
    AxiomsChecker,
    CompilationChecker,
    ForbiddenKeywordChecker,
)
from autoform.eval.types import load_task_list  # noqa: E402
from tools.execution.lean.constant import STANDARD_AXIOMS  # noqa: E402


async def main() -> None:
    workspace = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "runs/example/workspace"
    task_file = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO_ROOT / "runs/example/targets.yaml"

    workspace = workspace.resolve()
    task_file = task_file.resolve()

    targets = load_task_list(task_file)
    checked = [(t.lean_declaration, t.lean_file) for t in targets if t.lean_declaration and t.lean_file]
    decl_names = [d for d, _ in checked]
    lean_files = [f for _, f in checked]

    print(f"Workspace: {workspace}")
    print(f"Task file: {task_file}  ({len(targets)} targets, {len(decl_names)} declarations)")
    print()

    # --- Forbidden keywords (fast, no build) ---
    print("=== Forbidden Keywords ===")
    kw_checker = ForbiddenKeywordChecker(workspace)
    kw_violations = kw_checker.check()
    if kw_violations:
        for file, kw in kw_violations:
            print(f"  FAIL  {file}: {kw}")
    else:
        print("  PASS  No forbidden keywords found")
    print()

    # --- Compilation ---
    print("=== Compilation ===")
    comp_checker = CompilationChecker(workspace)
    compiled, comp_output = await comp_checker.check()
    status = "PASS" if compiled else "FAIL"
    print(f"  {status}  {'Build succeeded' if compiled else 'Build failed'}")
    if not compiled:
        print(f"  Output (first 500 chars): {comp_output[:500]}")
    print()

    # --- Axioms ---
    print("=== Axiom Checks ===")
    axiom_checker = AxiomsChecker(workspace)
    all_axioms, axiom_violations = await axiom_checker.check(decl_names, lean_files)
    for name in decl_names:
        axioms = all_axioms.get(name, set())
        bad = axiom_violations.get(name)
        if bad:
            print(f"  FAIL  {name}")
            print(f"        disallowed: {sorted(bad)}")
            if standard := axioms & STANDARD_AXIOMS:
                print(f"        standard:   {sorted(standard)}")
        elif axioms:
            print(f"  PASS  {name}: {sorted(axioms)}")
        else:
            print(f"  PASS  {name}: (no axioms)")
    print()

    # --- Summary ---
    all_ok = compiled and not kw_violations and not axiom_violations
    print("=== Summary ===")
    print(f"  Compilation:        {'PASS' if compiled else 'FAIL'}")
    print(f"  Forbidden keywords: {'PASS' if not kw_violations else f'FAIL ({len(kw_violations)} violations)'}")
    print(f"  Axiom checks:       {'PASS' if not axiom_violations else f'FAIL ({len(axiom_violations)} violations)'}")
    print(f"  Overall:            {'PASS' if all_ok else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
