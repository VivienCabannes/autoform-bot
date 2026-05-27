# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Mathlib source search tool.

Public API:
- find_mathlib_path: Locate Mathlib installation
- grep_mathlib, find_name_in_mathlib, read_mathlib_file: Search functions
- mathlib_server: MCPServerConfig factory
"""

from .core import find_mathlib_path, grep_mathlib, find_name_in_mathlib, read_mathlib_file
from .server import MathlibConfig, create_mathlib_server, mathlib_server

__all__ = [
    "MathlibConfig",
    "create_mathlib_server",
    "find_mathlib_path",
    "find_name_in_mathlib",
    "grep_mathlib",
    "mathlib_server",
    "read_mathlib_file",
]
