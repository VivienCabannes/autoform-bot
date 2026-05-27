# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Python version compatibility shims.

Centralizes conditional imports so they aren't duplicated across modules.
"""

from __future__ import annotations

import sys

# tomllib is in the standard library from Python 3.11+.
# For 3.10, fall back to the backport ``tomli``.
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

__all__ = ["tomllib"]
