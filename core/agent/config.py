# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Configuration for agents and model pricing."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..inference import InferenceConfig, ModelPricing, _MODEL_PRICING_REGISTRY


@dataclass
class AgentConfig:
    """Configuration for an Agent instance."""

    model: str = "Opus 4.6"
    inference_config: InferenceConfig = field(default_factory=InferenceConfig)
    context_window: int = 200_000
    compact_threshold: float = 0.75

    @property
    def pricing(self) -> ModelPricing:
        """Get pricing for this config's model from the global registry.

        Providers register their pricing via ``ModelPricing.register()``
        (called at import time in ``inference/client.py``).  This avoids
        an upward dependency from ``core/`` into ``inference/``.
        """
        return _MODEL_PRICING_REGISTRY.get(self.model, ModelPricing())
