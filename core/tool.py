# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tool metadata wrapper — adds autonomy scores and other metadata to tools."""

from __future__ import annotations


from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

MAX_RESULT_CHARS: float = 20_000


class Autonomy(StrEnum):
    """Agent autonomy levels, ordered by increasing capability."""

    BARE = "bare"
    READ = "read"
    WRITE = "write"
    EXECUTE_RESTRICTED = "execute-restricted"
    EXECUTE = "execute"

    @property
    def score(self) -> int:
        match self:
            case Autonomy.BARE:
                return 0
            case Autonomy.READ:
                return 10
            case Autonomy.WRITE:
                return 20
            case Autonomy.EXECUTE_RESTRICTED:
                return 25
            case Autonomy.EXECUTE:
                return 30

    @property
    def abbreviation(self) -> str:
        match self:
            case Autonomy.BARE:
                return "-"
            case Autonomy.READ:
                return "r"
            case Autonomy.WRITE:
                return "w"
            case Autonomy.EXECUTE_RESTRICTED:
                return "x-"
            case Autonomy.EXECUTE:
                return "x"

    @classmethod
    def max(cls) -> Autonomy:
        """Return the highest autonomy level."""
        return max(cls, key=lambda a: a.score)


@dataclass(frozen=True)
class ToolSpec:
    """Metadata wrapper for a tool function.

    Each ToolSpec registers itself in a class-level registry keyed by name.
    Use ``@ToolSpec.define(autonomy=Autonomy.EXECUTE)`` as a decorator on tool functions,
    or construct directly for external tools without source access.
    """

    name: str
    autonomy: Autonomy = Autonomy.EXECUTE
    max_result_chars: float = MAX_RESULT_CHARS

    _registry: ClassVar[dict[str, ToolSpec]] = {}

    def __post_init__(self) -> None:
        ToolSpec._registry[self.name] = self

    @classmethod
    def define(cls, *, autonomy: Autonomy = Autonomy.EXECUTE, max_result_chars: float = MAX_RESULT_CHARS):
        """Decorator that creates and registers a ToolSpec for the function.

        Usage::

            @server.tool
            @ToolSpec.define(autonomy=Autonomy.EXECUTE)
            def read_text_file(path: str) -> str: ...

        The function is returned unmodified.
        """

        def decorator(fn):
            cls(name=fn.__name__, autonomy=autonomy, max_result_chars=max_result_chars)
            return fn

        return decorator

    @classmethod
    def get(cls, name: str) -> ToolSpec | None:
        return cls._registry.get(name)

    @classmethod
    def autonomy_of(cls, name: str) -> Autonomy:
        """Return autonomy level for a tool.

        Defaults to the highest autonomy level (``Autonomy.EXECUTE``)
        for unknown tools so that unregistered tools are treated conservatively.
        """
        spec = cls._registry.get(name)
        return spec.autonomy if spec else Autonomy.max()

    @classmethod
    def max_result_chars_of(cls, name: str) -> float:
        """Return max_result_chars for a tool. Defaults to MAX_RESULT_CHARS for unknown tools."""
        spec = cls._registry.get(name)
        return spec.max_result_chars if spec else MAX_RESULT_CHARS

    @classmethod
    def compute_agent_autonomy(cls, tool_allowlist: list[str]) -> Autonomy:
        """Compute an agent's autonomy as the highest level among its tools.

        Returns the highest autonomy level (``Autonomy.EXECUTE``)
        when *tool_allowlist* is empty (no restriction).
        """
        if not tool_allowlist:
            return Autonomy.max()
        return max((cls.autonomy_of(t) for t in tool_allowlist), key=lambda a: a.score)
