# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for Model and inference helpers."""

from __future__ import annotations

import pytest

from .client import (
    DEFAULT_MODEL,
    _ALL_MODELS,
    _MODEL_BY_ABBR,
    Backend,
    Haiku_4_5,
    Model,
    Opus_4_6,
    Sonnet_4_6,
    lookup_model,
)


class TestModelAttributes:
    """Verify model attributes are set correctly."""

    def test_opus_model_name(self):
        assert Opus_4_6.model_name == "claude-opus-4-6"

    def test_opus_backend(self):
        assert Opus_4_6.backend == Backend.ANTHROPIC

    def test_sonnet_abbreviation(self):
        assert Sonnet_4_6.abbreviation == "Sonnet 4.6"

    def test_opus_abbreviation(self):
        assert Opus_4_6.abbreviation == "Opus 4.6"


class TestModelRepr:
    """Verify Model class __repr__ via metaclass."""

    def test_repr(self):
        assert "Opus 4.6" in repr(Opus_4_6)
        assert "claude-opus-4-6" in repr(Opus_4_6)

    def test_haiku_repr(self):
        assert "Haiku 4.5" in repr(Haiku_4_5)


class TestAutoDiscovery:
    """Verify models are auto-discovered via __init_subclass__."""

    def test_all_models_populated(self):
        assert len(_ALL_MODELS) > 0

    def test_models_registered(self):
        assert Opus_4_6 in _ALL_MODELS
        assert Sonnet_4_6 in _ALL_MODELS

    def test_model_by_abbr_uses_abbreviation(self):
        assert _MODEL_BY_ABBR["Opus 4.6"] is Opus_4_6

    def test_unique_abbreviations(self):
        """Every model has a unique abbreviation (required for lookup)."""
        abbrs = [m.abbreviation for m in _ALL_MODELS]
        assert len(abbrs) == len(set(abbrs)), f"Duplicate abbreviations: {[a for a in abbrs if abbrs.count(a) > 1]}"


class TestModelHelpers:
    """Verify module-level model helpers."""

    def test_default_model(self):
        assert DEFAULT_MODEL is Opus_4_6

    def test_lookup_model_valid(self):
        assert lookup_model("Opus 4.6") is Opus_4_6

    def test_lookup_model_invalid(self):
        with pytest.raises(ValueError, match="not available"):
            lookup_model("nonexistent-model")
