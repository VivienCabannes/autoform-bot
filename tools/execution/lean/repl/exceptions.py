# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Exception types for Lean REPL process lifecycle errors."""


class ReplProcessExited(RuntimeError):
    """REPL subprocess died unexpectedly (OOM, signal, EOF)."""


class ReplProcessRestarted(RuntimeError):
    """REPL died but was restarted. Callers should retry."""
