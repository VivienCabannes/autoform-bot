# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""vLLM server lifecycle management and singleton accessors.

Launches a vLLM OpenAI-compatible API server as a subprocess and
provides thread-safe singleton accessors for the server and an
AsyncOpenAI client that connects to it.
"""

from __future__ import annotations

import subprocess
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from logging import getLogger
from typing import ClassVar, TextIO

from openai import AsyncOpenAI

from core.process import PerRdvServer, RdvServerArgs, Singleton

logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Args & Server
# ---------------------------------------------------------------------------


@dataclass
class VLLMServerArgs(RdvServerArgs):
    """Configuration for launching a vLLM server."""

    NAME: ClassVar[str] = "vllm"

    python_interpreter: str = "python"
    request_timeout: float = 60.0
    num_request_retries: int = 3

    model: str = "mistralai/Leanstral-2603"
    tp_size: int = 1
    dp_size: int | None = None

    extra_args: dict[str, str | None] = field(default_factory=dict)


class VLLMServer(PerRdvServer):
    """Process-safe vLLM server (one instance per rendezvous point).

    Launches ``python -m vllm.entrypoints.openai.api_server`` with the
    configured model, tensor-parallel size, and data-parallel size.
    If ``dp_size`` is not set it is derived from the available GPU count.
    """

    def __init__(self, args: VLLMServerArgs) -> None:
        super().__init__(args)

        if args.dp_size is None:
            try:
                import torch
            except ImportError as exc:
                raise ImportError("torch is required to auto-detect dp_size for vLLM") from exc
            n_gpus = torch.cuda.device_count()
            if n_gpus == 0:
                raise RuntimeError("No CUDA GPUs detected; cannot start vLLM server")
            if n_gpus % args.tp_size != 0:
                raise ValueError(f"GPU count ({n_gpus}) is not divisible by tp_size ({args.tp_size})")
            args.dp_size = n_gpus // args.tp_size

    def _start_server_process(
        self,
        port: int,
        env: dict[str, str],
        stdout: TextIO,
        stderr: TextIO,
        args: VLLMServerArgs,  # type: ignore[override]
    ) -> str:
        cmd = [
            args.python_interpreter,
            "-m",
            "vllm.entrypoints.openai.api_server",
            f"--model={args.model}",
            f"--tensor-parallel-size={args.tp_size}",
            f"--data-parallel-size={args.dp_size}",
            f"--host={self._host}",
            f"--port={port}",
        ] + [f"--{k}" if v is None else f"--{k}={v}" for k, v in (args.extra_args or {}).items()]

        self.process = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, env=env)
        return f"http://{self._host}:{port}/v1"


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_thread_lock = threading.Lock()
_server_singleton: Singleton[VLLMServer] = Singleton(_thread_lock)


def get_vllm_server(args: VLLMServerArgs, context_stack: ExitStack) -> VLLMServer:
    """Get (or create) the singleton vLLM server instance."""
    return _server_singleton.get_instance(context_stack, VLLMServer, args)


def get_vllm_client(context_stack: ExitStack, *, base_url: str = "") -> AsyncOpenAI:
    """Get an AsyncOpenAI client pointing at the running vLLM server.

    If *base_url* is not provided, the address is read from the singleton
    server's address file.
    """
    if not base_url:
        server = _server_singleton.peek()
        if server is None:
            raise RuntimeError("vLLM server must be started before creating a client.")
        base_url = server.get_server_address()
    return AsyncOpenAI(api_key="no-key", base_url=base_url)


# ---------------------------------------------------------------------------
# Standalone CLI — exec vLLM directly (used by sbatch scripts)
# ---------------------------------------------------------------------------


def main() -> None:
    """Launch vLLM as a direct exec (no PerRdvServer machinery)."""
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description="Launch a vLLM OpenAI-compatible API server via exec.",
    )
    parser.add_argument("--python-interpreter", default="python")
    parser.add_argument("--model", required=True)
    parser.add_argument("--tp-size", type=int, default=1)
    parser.add_argument("--dp-size", type=int, default=1)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)

    args, extra = parser.parse_known_args()

    cmd = [
        args.python_interpreter,
        "-m",
        "vllm.entrypoints.openai.api_server",
        f"--model={args.model}",
        f"--tensor-parallel-size={args.tp_size}",
        f"--data-parallel-size={args.dp_size}",
        f"--host={args.host}",
        f"--port={args.port}",
    ] + extra

    logger.info("exec: %s", " ".join(cmd))
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
