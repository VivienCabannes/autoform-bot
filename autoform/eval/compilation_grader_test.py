# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for rubrics, programmatic graders, and jury grader."""

from __future__ import annotations

import asyncio


from core.eval.dtypes import Score
from core.eval.rubric import RubricSpec, load_json_rubrics, parse_judge_response

from autoform.eval.types import FormalizationTarget
from autoform.eval.compilation_grader import AxiomGrader, CompilationGrader, LLMJuryGrader, RUBRICS_DIR


# ---------------------------------------------------------------------------
# parse_judge_response
# ---------------------------------------------------------------------------


def test_parse_json_direct() -> None:
    score, reasoning, extra = parse_judge_response('{"score": 4, "reasoning": "Good work"}', 5)
    assert score == 4
    assert reasoning == "Good work"
    assert extra == {}


def test_parse_json_in_code_block() -> None:
    response = '```json\n{"score": 3, "reasoning": "Okay"}\n```'
    score, reasoning, _ = parse_judge_response(response, 5)
    assert score == 3
    assert reasoning == "Okay"


def test_parse_json_clamped_high() -> None:
    score, _, _ = parse_judge_response('{"score": 10, "reasoning": "Over"}', 5)
    assert score == 5  # clamped to max


def test_parse_json_clamped_low() -> None:
    score, _, _ = parse_judge_response('{"score": -2, "reasoning": "Under"}', 5)
    assert score == 0  # clamped to min


def test_parse_no_json() -> None:
    score, reasoning, _ = parse_judge_response("This is not JSON at all", 5)
    assert score == 0
    assert "No JSON" in reasoning


def test_parse_invalid_json() -> None:
    score, reasoning, _ = parse_judge_response("{broken json}", 5)
    assert score == 0
    assert "Invalid JSON" in reasoning


def test_parse_missing_score() -> None:
    score, reasoning, _ = parse_judge_response('{"reasoning": "No score field"}', 5)
    assert score == 0
    assert "Missing" in reasoning


def test_parse_extra_fields() -> None:
    response = '{"score": 4, "reasoning": "Good", "axiom_verdicts": {"sorry": {"justified": false, "explanation": "Book proves it"}}}'
    score, reasoning, extra = parse_judge_response(response, 5)
    assert score == 4
    assert "axiom_verdicts" in extra
    assert extra["axiom_verdicts"]["sorry"]["justified"] is False


# ---------------------------------------------------------------------------
# RubricSpec / JsonRubric
# ---------------------------------------------------------------------------


def test_load_rubric_spec_from_file() -> None:
    spec = RubricSpec.from_file(RUBRICS_DIR / "correctness.json")
    assert spec.name == "correctness"
    assert spec.active is True
    assert spec.weight == 0.4
    assert spec.max_score == 5
    assert spec.pass_threshold == 3
    assert 5 in spec.criteria
    assert 0 in spec.criteria


def test_load_rubrics_returns_all_active() -> None:
    rubrics = load_json_rubrics(RUBRICS_DIR)
    names = [r.name for r in rubrics]
    assert "correctness" in names
    assert "faithfulness" in names
    assert "style" in names


# ---------------------------------------------------------------------------
# Rubric prompt generation
# ---------------------------------------------------------------------------

_SAMPLE_TARGET = FormalizationTarget(
    name="Theorem 3.3 (Hahn Decomposition)",
    lean_declaration="hahn_decomposition",
    lean_file="RealAnalysis/Measure/Hahn.lean",
    description="If nu is a signed measure, there exist positive and negative sets.",
)

_SAMPLE_SOURCE = "import Mathlib\n\ntheorem hahn_decomposition : True := trivial"


def test_correctness_rubric_prompt() -> None:
    rubrics = load_json_rubrics(RUBRICS_DIR)
    rubric = next(r for r in rubrics if r.name == "correctness")
    prompt = rubric.prompt(_SAMPLE_TARGET, _SAMPLE_SOURCE)
    assert "mathematical correctness" in prompt.lower()
    assert _SAMPLE_TARGET.description in prompt
    assert _SAMPLE_SOURCE in prompt


def test_faithfulness_rubric_prompt() -> None:
    rubrics = load_json_rubrics(RUBRICS_DIR)
    rubric = next(r for r in rubrics if r.name == "faithfulness")
    prompt = rubric.prompt(_SAMPLE_TARGET, _SAMPLE_SOURCE)
    assert "faithfulness" in prompt.lower()
    assert _SAMPLE_TARGET.description in prompt


def test_style_rubric_prompt() -> None:
    rubrics = load_json_rubrics(RUBRICS_DIR)
    rubric = next(r for r in rubrics if r.name == "style")
    prompt = rubric.prompt(_SAMPLE_TARGET, _SAMPLE_SOURCE)
    assert "style" in prompt.lower()
    assert _SAMPLE_SOURCE in prompt


# ---------------------------------------------------------------------------
# Rubric process_answer
# ---------------------------------------------------------------------------


def test_correctness_process_answer() -> None:
    rubrics = load_json_rubrics(RUBRICS_DIR)
    rubric = next(r for r in rubrics if r.name == "correctness")
    score = rubric.process_answer('{"score": 4, "reasoning": "Good"}')
    assert score.value == 4 / 5
    assert score.passed is True
    assert score.metrics["correctness"] == 4


def test_faithfulness_below_threshold() -> None:
    rubrics = load_json_rubrics(RUBRICS_DIR)
    rubric = next(r for r in rubrics if r.name == "faithfulness")
    score = rubric.process_answer('{"score": 2, "reasoning": "Extra hypotheses"}')
    assert score.passed is False
    assert score.metrics["faithfulness"] == 2


# ---------------------------------------------------------------------------
# CompilationGrader
# ---------------------------------------------------------------------------


def test_compilation_grader_pass() -> None:
    grader = CompilationGrader.create(compiled=True, compilation_output="Build successful")
    score = asyncio.get_event_loop().run_until_complete(grader.grade(_SAMPLE_TARGET, _SAMPLE_SOURCE))
    assert score.passed is True
    assert score.value == 1.0
    assert score.metrics["compilation"] == 1
    assert score.feedback == ""


def test_compilation_grader_fail() -> None:
    grader = CompilationGrader.create(compiled=False, compilation_output="error: unknown identifier")
    score = asyncio.get_event_loop().run_until_complete(grader.grade(_SAMPLE_TARGET, _SAMPLE_SOURCE))
    assert score.passed is False
    assert score.value == 0.0
    assert score.metrics["compilation"] == 0
    assert "error:" in score.feedback


# ---------------------------------------------------------------------------
# AxiomGrader
# ---------------------------------------------------------------------------


def test_axiom_grader_clean() -> None:
    grader = AxiomGrader(violations={})
    score = asyncio.get_event_loop().run_until_complete(grader.grade(_SAMPLE_TARGET, _SAMPLE_SOURCE))
    assert score.passed is True
    assert score.value == 1.0
    assert score.metrics["axioms"] == 1


def test_axiom_grader_violation() -> None:
    grader = AxiomGrader(violations={"hahn_decomposition": frozenset({"sorryAx"})})
    score = asyncio.get_event_loop().run_until_complete(grader.grade(_SAMPLE_TARGET, _SAMPLE_SOURCE))
    assert score.passed is False
    assert score.value == 0.0
    assert score.metrics["axioms"] == 0
    assert "sorryAx" in score.feedback


def test_axiom_grader_unrelated_violation() -> None:
    """Violations for other declarations don't affect this target."""
    grader = AxiomGrader(violations={"other_decl": frozenset({"sorryAx"})})
    score = asyncio.get_event_loop().run_until_complete(grader.grade(_SAMPLE_TARGET, _SAMPLE_SOURCE))
    assert score.passed is True


# ---------------------------------------------------------------------------
# LLMJuryGrader.aggregate
# ---------------------------------------------------------------------------


def test_aggregate_all_pass() -> None:
    grader = LLMJuryGrader(
        members=[],
        _weights={
            "correctness": 0.4,
            "faithfulness": 0.4,
            "style": 0.2,
        },
    )
    scores = {
        "correctness": Score(value=4 / 5, passed=True, feedback="ok", metrics={"correctness": 4}),
        "faithfulness": Score(value=5 / 5, passed=True, feedback="ok", metrics={"faithfulness": 5}),
        "style": Score(value=3 / 5, passed=True, feedback="ok", metrics={"style": 3}),
    }
    result = grader.aggregate(scores)
    assert result.passed is True
    # weighted: 0.4*4/5 + 0.4*5/5 + 0.2*3/5 = 0.32 + 0.40 + 0.12 = 0.84
    assert abs(result.value - 0.84) < 0.01
    assert result.metrics["correctness"] == 4
    assert result.metrics["faithfulness"] == 5
    assert result.metrics["style"] == 3


def test_aggregate_one_fails() -> None:
    grader = LLMJuryGrader(
        members=[],
        _weights={
            "correctness": 0.4,
            "faithfulness": 0.4,
            "style": 0.2,
        },
    )
    scores = {
        "correctness": Score(value=4 / 5, passed=True, feedback="ok", metrics={"correctness": 4}),
        "faithfulness": Score(value=1 / 5, passed=False, feedback="bad", metrics={"faithfulness": 1}),
        "style": Score(value=4 / 5, passed=True, feedback="ok", metrics={"style": 4}),
    }
    result = grader.aggregate(scores)
    assert result.passed is False
