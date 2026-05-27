# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean code analysis — sorry detection and codebase inspection."""

from .core import find_sorries
from .server import lean_analysis_server

__all__ = ["find_sorries", "lean_analysis_server"]
