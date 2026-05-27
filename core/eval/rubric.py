# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""JSON-driven rubrics and judge response parsing.

Provides a generic ``JsonRubric`` that loads scoring criteria from JSON
files and works with any dataclass datum type.  The JSON schema is::

    {
        "name": "...",
        "description": "...",
        "active": true,
        "weight": 0.4,
        "pass_threshold": 3,
        "max_score": 5,
        "criteria": {"5": "...", "4": "...", ...},
        "prompt_template": "... {field} ... {output} ... {criteria} ..."
    }

Template placeholders are resolved from ``dataclasses.asdict(datum)``
merged with ``{"output": output, "criteria": <formatted criteria>}``.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import build_with_type_check

from .dtypes import Score
from .grader import Rubric


# ---------------------------------------------------------------------------
# Judge response parsing
# ---------------------------------------------------------------------------


def _escape_control_in_strings(s: str) -> str:
    """Escape literal newlines/tabs inside JSON string values.

    JSON spec forbids unescaped control characters.  This walks the
    string tracking whether we are inside a quoted value and replaces
    raw ``\n``, ``\r``, ``\t`` with their escaped forms.
    """
    parts = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            parts.append(ch)
            escape_next = False
            continue
        if ch == "\\" and in_string:
            parts.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            parts.append(ch)
            continue
        if in_string and ch == "\n":
            parts.append("\\n")
        elif in_string and ch == "\r":
            parts.append("\\r")
        elif in_string and ch == "\t":
            parts.append("\\t")
        else:
            parts.append(ch)
    return "".join(parts)


def parse_judge_response(response: str, max_score: int) -> tuple[int, str, dict[str, Any]]:
    """Extract ``(score, reasoning, extra_fields)`` from an LLM judge response.

    Handles raw JSON, JSON inside markdown fences, and malformed responses.
    Returns ``(0, error_message, {})`` on parse failure. Extra fields beyond
    ``score`` and ``reasoning`` are returned in the third element.
    """
    fence_match = re.search(r"```json\s*\n?([\s\S]*?)```", response)
    text = fence_match.group(1).strip() if fence_match else response.strip()

    json_match = re.search(r"\{[\s\S]*\}", text)
    if not json_match:
        return 0, f"No JSON found in response: {response[:200]}", {}

    raw = json_match.group()

    sanitized = _escape_control_in_strings(raw)
    try:
        data = json.loads(sanitized)
    except json.JSONDecodeError as e:
        return 0, f"Invalid JSON: {e}", {}

    score = data.get("score")
    reasoning = data.get("reasoning", "")

    if not isinstance(score, (int, float)):
        return 0, f"Missing or invalid 'score' field: {data}", {}

    score = max(0, min(max_score, int(score)))
    extra = {k: v for k, v in data.items() if k not in ("score", "reasoning")}
    return score, str(reasoning), extra


# ---------------------------------------------------------------------------
# Rubric spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RubricSpec:
    """Parsed rubric specification from a JSON file."""

    name: str
    description: str
    active: bool
    weight: float
    pass_threshold: int
    max_score: int
    criteria: dict[int, str]
    prompt_template: str

    @classmethod
    def from_file(cls, path: Path) -> RubricSpec:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return build_with_type_check(cls, data)


# ---------------------------------------------------------------------------
# JSON-driven rubric
# ---------------------------------------------------------------------------


class JsonRubric(Rubric):
    """Rubric driven by a ``RubricSpec`` loaded from JSON.

    Works with any dataclass datum — template variables are extracted via
    ``dataclasses.asdict(datum)`` and merged with ``output`` and
    ``criteria``.
    """

    def __init__(self, spec: RubricSpec) -> None:
        self.spec = spec
        self.name = spec.name

    def prompt(self, datum: object, output: str) -> str:
        criteria_lines = "\n".join(
            f"- **{score}**: {desc}" for score, desc in sorted(self.spec.criteria.items(), reverse=True)
        )
        fields = dataclasses.asdict(datum)
        fields["output"] = output
        fields["criteria"] = criteria_lines
        return self.spec.prompt_template.format_map(fields)

    def process_answer(self, response: str) -> Score:
        score, reasoning, extra = parse_judge_response(response, self.spec.max_score)
        metrics: dict[str, Any] = {self.spec.name: score}
        metrics.update(extra)
        return Score(
            value=score / self.spec.max_score,
            passed=score >= self.spec.pass_threshold,
            feedback=reasoning,
            metrics=metrics,
        )


# ---------------------------------------------------------------------------
# Lenient JSON parsing
# ---------------------------------------------------------------------------

_logger = __import__("logging").getLogger(__name__)


def _fix_json(text: str) -> str:
    """Fix common JSON issues from LLM responses.

    Handles unquoted property names, single-quoted keys/values,
    and trailing commas.
    """
    # Fix unquoted keys: {score: 4} → {"score": 4}
    text = re.sub(r"(?<=[{,\n])\s*(\w+)\s*:", r' "\1":', text)
    # Fix single-quoted keys: {'score': 4} → {"score": 4}
    text = re.sub(r"'(\w+)'\s*:", r'"\1":', text)
    # Fix single-quoted string values: "reasoning": 'text' → "reasoning": "text"
    text = re.sub(r":\s*'([^']*)'", r': "\1"', text)
    # Remove trailing commas before }
    text = re.sub(r",\s*}", "}", text)
    return text


def _extract_score_json(text: str) -> dict | None:
    """Extract a score/reasoning JSON from text that may contain LaTeX/Lean code.

    Specifically looks for ``{"score": N, "reasoning": "..."}`` patterns,
    tolerating unquoted keys.
    """
    # Try exact JSON patterns first
    for pattern in [
        r'\{\s*"score"\s*:\s*(\d+)\s*,\s*"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
        r'\{\s*score\s*:\s*(\d+)\s*,\s*reasoning\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
        r"\{\s*score\s*:\s*(\d+)\s*,\s*reasoning\s*:\s*'((?:[^'\\]|\\.)*)'\s*\}",
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            return {"score": int(m.group(1)), "reasoning": m.group(2)}

    # Last resort: find any {"score": N, ...} and try to parse it
    m = re.search(r'\{\s*"?score"?\s*:\s*\d+', text)
    if m:
        # Find the matching closing brace, skipping braces inside strings
        start = m.start()
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    sanitized = _escape_control_in_strings(candidate)
                    try:
                        return json.loads(sanitized)
                    except json.JSONDecodeError:
                        pass
                    # Retry with _fix_json for unquoted keys
                    fixed = _fix_json(sanitized)
                    try:
                        return json.loads(fixed)
                    except json.JSONDecodeError:
                        pass
                    break

    return None


class LenientJsonRubric(JsonRubric):
    """JsonRubric with more lenient response parsing.

    Falls back to fixing unquoted keys before giving up on JSON parsing.
    Useful when LLM judges return JSON embedded in text that contains
    LaTeX or Lean code with ``{`` ``}`` characters.
    """

    def process_answer(self, response: str) -> Score:
        # First try standard parsing
        result = super().process_answer(response)
        if result.value > 0 or "Invalid JSON" not in result.feedback:
            return result

        # Retry with targeted JSON extraction — look for {"score" or {score
        # patterns instead of any {...} (which matches LaTeX/Lean code)
        data = _extract_score_json(response)
        if data is None:
            _logger.warning("Could not extract score JSON for %s from: %s", self.name, response[:200])
            return result

        score = data.get("score")
        reasoning = data.get("reasoning", "")

        if not isinstance(score, (int, float)):
            return result

        score = max(0, min(self.spec.max_score, int(score)))
        metrics: dict[str, Any] = {self.spec.name: score}
        extra = {k: v for k, v in data.items() if k not in ("score", "reasoning")}
        metrics.update(extra)
        return Score(
            value=score / self.spec.max_score,
            passed=score >= self.spec.pass_threshold,
            feedback=str(reasoning),
            metrics=metrics,
        )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_json_rubrics(rubrics_dir: Path) -> list[JsonRubric]:
    """Load all active rubric JSON files from a directory as ``JsonRubric``."""
    specs = [RubricSpec.from_file(p) for p in sorted(rubrics_dir.glob("*.json"))]
    return [JsonRubric(spec) for spec in specs if spec.active]


def load_lenient_rubrics(rubrics_dir: Path) -> list[LenientJsonRubric]:
    """Load all active rubric JSON files with lenient parsing."""
    specs = [RubricSpec.from_file(p) for p in sorted(rubrics_dir.glob("*.json"))]
    return [LenientJsonRubric(spec) for spec in specs if spec.active]
