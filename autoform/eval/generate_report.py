# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Generate a markdown report and failed_targets.yaml from the JSON assessment output.

Usage:
    python generate_report.py <report.json> [--targets <targets.yaml>]
"""

import json
import re
import sys
from pathlib import Path

import yaml


def _detail_row(d: dict) -> str:
    """Format a single detail entry as a markdown table row."""
    sc = d.get("scores", {})
    return (
        f"| {d.get('idx', '?')} "
        f"| {d.get('name', '?')} | {d.get('kind', '-')} | {d.get('location', '-')} "
        f"| `{d.get('lean_declaration', '-')}` | `{d.get('lean_file', '-')}` "
        f"| {sc.get('faithfulness', '-')} | {sc.get('proof_integrity', '-')} | {sc.get('code_quality', '-')} |"
    )


def _issue_detail(d: dict) -> list[str]:
    """Format a single issue entry with metadata and failing judge feedback."""
    lines: list[str] = []
    sc = d.get("scores", {})
    reason = f"faith={sc.get('faithfulness', '-')}, integrity={sc.get('proof_integrity', '-')}, quality={sc.get('code_quality', '-')}"
    lines.append(f"### [{d.get('idx', '?')}] {d.get('name', '?')} — {reason}\n")
    if d.get("kind"):
        lines.append(f"- **Kind:** {d['kind']}")
    if d.get("location"):
        lines.append(f"- **Location:** {d['location']}")
    if d.get("description"):
        lines.append(f"- **Book statement:** {d['description']}")
    if d.get("lean_declaration") and d["lean_declaration"] != "-":
        lines.append(f"- **Declaration:** `{d['lean_declaration']}`")
    if d.get("lean_file") and d["lean_file"] != "-":
        lines.append(f"- **File:** `{d['lean_file']}`")
    if d.get("axioms"):
        lines.append(f"- **Axioms:** {d['axioms']}")
    lines.append("")

    feedback = d.get("feedback", "").strip()
    if feedback:
        # Split per-rubric feedback and only show failing rubrics (score < 3)
        rubric_blocks = re.split(r"(?=\[[\w_]+=\d+/\d+\])", feedback)
        for block in rubric_blocks:
            block = block.strip()
            if not block:
                continue
            tag_match = re.match(r"\[([\w_]+)=(\d+)/\d+\]", block)
            if tag_match:
                score = int(tag_match.group(2))
                if score >= 3:
                    continue
            for fb_line in block.split("\n"):
                lines.append(f"> {fb_line}")
            lines.append("")
    return lines


def generate(json_path: str, targets_path: str | None = None) -> None:
    report_path = Path(json_path)
    with open(report_path) as f:
        report = json.load(f)

    details = report["statements"]["details"]
    s = report["statements"]["summary"]
    repo = report["repo"]

    # Categorize targets into three groups
    passed: list[dict] = []
    issues: list[dict] = []
    not_covered: list[dict] = []

    for d in details:
        if d["passed"]:
            passed.append(d)
        elif d.get("match_confidence") == "not_found" or (
            not d.get("lean_declaration") or d.get("lean_declaration") == "-"
        ):
            not_covered.append(d)
        else:
            issues.append(d)

    # --- Markdown report ---
    out = report_path.with_suffix(".md")
    lines: list[str] = []

    lines.append("# Formalization Assessment Report\n")
    lines.append(f"- **Compiles:** {repo['compiles']}")
    lines.append(
        f"- **Total:** {s['total']} | **Passed:** {len(passed)} "
        f"| **Issues:** {len(issues)} | **Not covered:** {len(not_covered)} "
        f"| **Pass rate:** {s['pass_rate']:.1%}"
    )
    for k in ("faithfulness", "proof_integrity", "code_quality"):
        if k in s:
            lines.append(f"- **{k.replace('_', ' ').capitalize()}** (avg): {s[k]}/5")
    lines.append("")
    lines.append("### Pass thresholds")
    lines.append("")
    lines.append("A target passes only if **all three** rubrics meet their individual thresholds:")
    lines.append("")
    lines.append("| Rubric | Threshold | Weight |")
    lines.append("|---|---|---|")
    lines.append("| Faithfulness | ≥ 4/5 | 40% |")
    lines.append("| Proof integrity | ≥ 3/5 | 40% |")
    lines.append("| Code quality | ≥ 3/5 | 20% |")
    lines.append("")

    # Section 1: Issues — matched but judge flagged problems (PRIORITY)
    lines.append(f"## Issues ({len(issues)})\n")
    if issues:
        lines.append(
            "Targets that have a matching declaration but failed evaluation. **These need fixes — focus here first.**\n"
        )
        for d in issues:
            lines.extend(_issue_detail(d))
    else:
        lines.append("No issues found.\n")

    # Section 2: Not covered — no matching declaration found
    lines.append(f"## Not Covered ({len(not_covered)})\n")
    if not_covered:
        lines.append(
            "Targets with no matching declaration in the codebase. These need tasks created to formalize them.\n"
        )
        lines.append("| Idx | Statement | Kind | Location |")
        lines.append("|---|---|---|---|")
        for d in not_covered:
            lines.append(
                f"| {d.get('idx', '?')} | {d.get('name', '?')} | {d.get('kind', '-')} | {d.get('location', '-')} |"
            )
        lines.append("")
    else:
        lines.append("All targets are covered.\n")

    # Section 3: Passed — brief summary table
    lines.append(f"## Passed ({len(passed)})\n")
    if passed:
        lines.append("| Idx | Statement | Kind | Location | Declaration | File | Faith | Integrity | Quality |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for d in passed:
            lines.append(_detail_row(d))
        lines.append("")
    else:
        lines.append("No targets passed yet.\n")

    with open(out, "w") as f:
        f.write("\n".join(lines))
    print(f"Report: {out}")

    # --- Failed targets ---
    failed = issues + not_covered
    if failed and targets_path:
        with open(targets_path) as f:
            all_targets = yaml.safe_load(f)

        failed_indices = {d.get("idx") for d in failed if d.get("idx") is not None}
        failed_targets = [t for i, t in enumerate(all_targets) if i in failed_indices]

        failed_path = report_path.parent / "failed_targets.yaml"
        with open(failed_path, "w") as f:
            yaml.dump(failed_targets, f, default_flow_style=False, allow_unicode=True)
        print(f"Failed targets ({len(failed_targets)}): {failed_path}")


if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else "autoform/eval/output/report.json"
    targets_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--targets" and i + 1 < len(sys.argv):
            targets_path = sys.argv[i + 1]
    generate(json_path, targets_path)
