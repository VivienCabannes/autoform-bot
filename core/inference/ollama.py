# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Ollama server lifecycle management and singleton accessors.

Launches ``ollama serve`` as a subprocess and provides thread-safe
singleton accessors for the server and an AsyncOpenAI client that
connects to its OpenAI-compatible ``/v1`` endpoint.
"""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from logging import getLogger
from typing import ClassVar, TextIO

from openai import AsyncOpenAI

from core.process import PerRdvServer, RdvServerArgs, Singleton

logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Args & Server
# ---------------------------------------------------------------------------


@dataclass
class OllamaServerArgs(RdvServerArgs):
    """Configuration for launching an Ollama server.

    Any field whose name starts with ``OLLAMA_`` is forwarded as an
    environment variable to the ``ollama serve`` process (e.g.
    ``OLLAMA_MODELS``, ``OLLAMA_NUM_PARALLEL``).
    """

    NAME: ClassVar[str] = "ollama"

    ollama_binary: str = "ollama"

    def get_server_env_vars(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for f in self.__dataclass_fields__.values():
            if f.name.startswith("OLLAMA_"):
                val = getattr(self, f.name)
                if val is not None:
                    env[f.name] = str(os.path.expandvars(val) if isinstance(val, str) else val)
        return env


class OllamaServer(PerRdvServer):
    """Process-safe Ollama server (one instance per rendezvous point).

    Launches ``ollama serve`` with ``OLLAMA_HOST`` set to the chosen
    host:port so that it listens on a dynamically allocated port.
    """

    def __init__(self, args: OllamaServerArgs) -> None:
        super().__init__(args)

    def _start_server_process(
        self,
        port: int,
        env: dict[str, str],
        stdout: TextIO,
        stderr: TextIO,
        args: OllamaServerArgs,  # type: ignore[override]
    ) -> str:
        env["OLLAMA_HOST"] = f"{self._host}:{port}"
        self.process = subprocess.Popen(
            [args.ollama_binary, "serve"],
            stdout=stdout,
            stderr=stderr,
            env=env,
        )
        return f"http://{self._host}:{port}/v1"


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_thread_lock = threading.Lock()
_server_singleton: Singleton[OllamaServer] = Singleton(_thread_lock)


def get_ollama_server(args: OllamaServerArgs, context_stack: ExitStack) -> OllamaServer:
    """Get (or create) the singleton Ollama server instance."""
    return _server_singleton.get_instance(context_stack, OllamaServer, args)


def get_ollama_client(context_stack: ExitStack, *, base_url: str = "") -> AsyncOpenAI:
    """Get an AsyncOpenAI client pointing at the running Ollama server.

    If *base_url* is not provided, the address is read from the singleton
    server's address file.
    """
    if not base_url:
        server = _server_singleton.peek()
        if server is None:
            raise RuntimeError("Ollama server must be started before creating a client.")
        base_url = server.get_server_address()
    return AsyncOpenAI(api_key="no-key", base_url=base_url)
