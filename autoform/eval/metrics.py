# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Autoformalization evaluation — report builder.

The grading primitives live in their own modules:
- ``lean_checks`` — compilation, forbidden keywords, axiom usage.
- ``rubrics`` — per-statement LLM-judged rubrics (correctness, faithfulness, style).

This module provides the report builder that combines both into a single
output dict.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.eval.dtypes import EvalResult
from core.inference import InferenceProtocol

InferenceFactory = Callable[[], InferenceProtocol]


_DEFAULT_METRIC_KEYS = ("compilation", "axioms", "correctness", "faithfulness", "style")
_DEFAULT_DETAIL_FIELDS = ("name", "lean_declaration")


def build_report(
    *,
    compiles: bool,
    compilation_output: str,
    forbidden_keyword_violations: list[tuple[str, str]],
    axiom_violations: dict[str, frozenset[str]] | None = None,
    results: list[EvalResult],
    metric_keys: tuple[str, ...] = _DEFAULT_METRIC_KEYS,
    detail_fields: tuple[str, ...] = _DEFAULT_DETAIL_FIELDS,
) -> dict[str, Any]:
    """Combine repo-level and statement-level results into a single report.

    Args:
        metric_keys: Which metric names to include in summaries and per-statement scores.
        detail_fields: Which attribute names to extract from each result's datum
            into the detail entry.
    """
    total = len(results)
    passed = sum(1 for r in results if r.score.passed)

    summary: dict[str, Any] = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 3) if total else 0.0,
    }
    for key in metric_keys:
        values = [r.score.metrics.get(key) for r in results if key in r.score.metrics]
        if values:
            summary[key] = round(sum(values) / len(values), 2)

    details: list[dict[str, Any]] = []
    for r in results:
        entry: dict[str, Any] = {
            "id": r.datum_id,
            "passed": r.score.passed,
            "scores": {k: v for k, v in r.score.metrics.items() if k in metric_keys},
            "feedback": r.score.feedback,
        }
        if "axiom_verdicts" in r.score.metrics:
            entry["axiom_verdicts"] = r.score.metrics["axiom_verdicts"]
        if "axiom_only" in r.score.metrics:
            entry["axiom_only"] = r.score.metrics["axiom_only"]
        if r.datum is not None:
            for field in detail_fields:
                val = getattr(r.datum, field, None)
                if val is not None:
                    entry[field] = val
        details.append(entry)

    details.sort(key=lambda d: (d["passed"], d.get("id", "")))

    all_checks_passed = compiles and not forbidden_keyword_violations and not (axiom_violations or {})

    repo: dict[str, Any] = {
        "compiles": compiles,
        "compilation_output": compilation_output,
        "forbidden_keyword_violations": [{"file": f, "keyword": kw} for f, kw in forbidden_keyword_violations],
        "all_checks_passed": all_checks_passed,
    }
    if axiom_violations is not None:
        repo["axiom_violations"] = [
            {"declaration": name, "disallowed_axioms": sorted(axs)} for name, axs in axiom_violations.items()
        ]

    return {
        "repo": repo,
        "statements": {
            "summary": summary,
            "details": details,
        },
    }
