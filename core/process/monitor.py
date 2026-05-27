# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Process utilities — subprocess cleanup, environment setup, memory monitoring."""

import os
import subprocess
import threading

import psutil

from .pool import MemoryUnit

DEFAULT_KILL_TIMEOUT = 5.0
DEFAULT_CHECK_INTERVAL = 10.0
DEFAULT_JOIN_TIMEOUT = 2.0


def kill_subprocesses(process: subprocess.Popen, timeout: float = DEFAULT_KILL_TIMEOUT) -> None:
    """Gracefully terminate a process tree, force-killing survivors."""
    try:
        p = psutil.Process(process.pid)
    except psutil.NoSuchProcess:
        return

    children = p.children(recursive=True)

    for c in children:
        try:
            c.terminate()
        except psutil.NoSuchProcess:
            pass
    p.terminate()

    _, alive = psutil.wait_procs(children, timeout=timeout)
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, psutil.NoSuchProcess):
        pass

    for c in alive:
        try:
            c.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        p.kill()
        p.wait()
    except psutil.NoSuchProcess:
        pass

    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            if stream:
                stream.close()
        except Exception:
            pass


def inherit_clean_env() -> dict[str, str]:
    """Return the current environment with distributed-training vars stripped.

    Also disables the WANDB service agent to prevent it from interfering
    with subprocess lifecycle.
    """
    skip_prefixes = ("SLURM_", "SLURMD_", "SRUN_", "SBATCH_", "SUBMITIT_", "TORCHELASTIC_")
    skip_names = {
        "MASTER_ADDR",
        "MASTER_PORT",
        "RANK",
        "WORLD_SIZE",
        "LOCAL_RANK",
        "LOCAL_WORLD_SIZE",
        "TMPDIR",
        "TMP_DIR",
        "TRITON_CACHE_DIR",
        "TORCHINDUCTOR_CACHE_DIR",
    }
    env: dict[str, str] = {}
    for key, val in os.environ.items():
        if key.startswith(skip_prefixes) or key in skip_names:
            continue
        env[key] = val
    env["WANDB_DISABLE_SERVICE"] = "True"
    return env


def get_process_memory_usage(
    process: subprocess.Popen | None,
    unit: MemoryUnit = "B",
) -> float:
    """Get total memory usage of a process and all its children."""
    _unit_divisor: int = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}[unit]
    if process is None or process.poll() is not None:
        return 0.0
    try:
        p = psutil.Process(process.pid)
        total = p.memory_info().rss
        for child in p.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        return total / _unit_divisor
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0


class MemoryMonitor:
    """Monitor a subprocess and kill it if memory usage exceeds the limit."""

    def __init__(
        self,
        process: subprocess.Popen,
        mem_limit_gb: float,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ):
        self.process = process
        self.mem_limit_bytes = mem_limit_gb * (1024**3)
        self.check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.exceeded = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=DEFAULT_JOIN_TIMEOUT)

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.process.poll() is not None:
                break
            mem_usage = get_process_memory_usage(self.process, unit="B")
            if mem_usage > self.mem_limit_bytes:
                self.exceeded = True
                mem_gb = mem_usage / (1024**3)
                limit_gb = self.mem_limit_bytes / (1024**3)
                msg = f"Memory limit exceeded: {mem_gb:.2f}GB > {limit_gb:.2f}GB. Killing process.\n"
                try:
                    os.write(2, msg.encode("utf-8"))
                except OSError:
                    pass
                kill_subprocesses(self.process)
                break
            self._stop_event.wait(self.check_interval)

    def __enter__(self) -> "MemoryMonitor":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()
