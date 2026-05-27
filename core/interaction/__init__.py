# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Bidirectional external interaction for agents.

Defines the Interaction lifecycle: frame → deliver → respond → react.
Provides AgentRegistry for routing interactive messages to running agents.
"""

from .handler import Interaction
from .registry import AgentRegistry, get_registry

__all__ = ["AgentRegistry", "Interaction", "get_registry"]
