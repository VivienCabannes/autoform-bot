# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""core.eval — Generic typed evaluation pipeline.

Provides Dataset[D], Grader[D, O], MetricMonitor, and EvalPipeline
for building type-safe, composable evaluation workflows.
"""

from .dtypes import Datum, EvalResult, Output, Score
from .grader import Grader, JuryGrader, LLMJudgeGrader, Rubric
from .metrics import (
    BUILTIN_AGGREGATORS,
    Aggregator,
    EvalAggregator,
    GroupedEvalResults,
    MetricMonitor,
    ResultCallback,
    mean_metric,
    pass_at_k,
)
from .pipeline import AgentRunner, Dataset, EvalPipeline, SimpleAgentRunner
from .rubric import (
    JsonRubric,
    LenientJsonRubric,
    RubricSpec,
    load_json_rubrics,
    load_lenient_rubrics,
    parse_judge_response,
)

__all__ = [
    "AgentRunner",
    "Aggregator",
    "BUILTIN_AGGREGATORS",
    "Dataset",
    "Datum",
    "EvalAggregator",
    "EvalPipeline",
    "EvalResult",
    "Grader",
    "GroupedEvalResults",
    "JsonRubric",
    "JuryGrader",
    "LLMJudgeGrader",
    "LenientJsonRubric",
    "MetricMonitor",
    "Output",
    "ResultCallback",
    "Rubric",
    "RubricSpec",
    "Score",
    "SimpleAgentRunner",
    "load_json_rubrics",
    "load_lenient_rubrics",
    "mean_metric",
    "parse_judge_response",
    "pass_at_k",
]
