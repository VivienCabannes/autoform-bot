# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Constrained task tracker — orchestrator-safe wrapper around ItemTracker."""

from .core import ConstrainedTracker
from .server import constrained_tracker_server

# Backward-compatible alias used by orchestration.py.
task_tracker_server = constrained_tracker_server

__all__ = ["ConstrainedTracker", "constrained_tracker_server", "task_tracker_server"]
