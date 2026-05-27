# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Generic ZMQ task queue for multi-node coordination.

Provides a ROUTER/DEALER socket pair so a coordinator can dispatch tasks to
specific workers and receive results back over the same channel.

Also provides SLURM environment utilities for rank/world-size detection.

Message protocol (plain dicts over JSON):

    # worker → coordinator
    {"type": "register", "rank": 2, "capacity": 5}
    {"type": "ack",      "rank": 2, "task_id": "..."}
    {"type": "result",   "rank": 2, "task_id": "...", "success": True,
                         "winner_id": "...", "error": None, "capacity": 4}

    # coordinator → worker
    {"type": "task",   "task_id": "...", "title": "...",
                       "description": "...", "attempt_number": 1}
    {"type": "cancel", "task_id": "..."}
"""

from __future__ import annotations

import re
import os
from collections import deque


# ---------------------------------------------------------------------------
# SLURM utilities
# ---------------------------------------------------------------------------


def get_rank() -> int:
    """Return the global rank of this process (0-based).

    Checks SLURM_PROCID, then RANK, then defaults to 0.
    """
    return int(os.environ.get("SLURM_PROCID", os.environ.get("RANK", "0")))


def get_world_size() -> int:
    """Return the total number of processes in this job.

    Checks SLURM_NTASKS, then WORLD_SIZE, then defaults to 1.
    """
    return int(os.environ.get("SLURM_NTASKS", os.environ.get("WORLD_SIZE", "1")))


def get_master_addr() -> str:
    """Return the hostname of the coordinator (rank 0).

    Checks MASTER_ADDR first, then parses the first node from SLURM_NODELIST.
    Falls back to "localhost".
    """
    addr = os.environ.get("MASTER_ADDR", "")
    if addr:
        return addr
    nodelist = os.environ.get("SLURM_NODELIST", "")
    if nodelist:
        nodes = parse_nodelist(nodelist)
        if nodes:
            return nodes[0]
    return "localhost"


def get_master_port() -> int:
    """Return the port the coordinator listens on. Checks MASTER_PORT, defaults to 29500."""
    return int(os.environ.get("MASTER_PORT", "29500"))


def is_distributed() -> bool:
    """Return True if world_size > 1 (running in a multi-node job)."""
    return get_world_size() > 1


def parse_nodelist(nodelist: str) -> list[str]:
    """Expand a SLURM compact nodelist into individual hostnames.

    Handles formats like:
        node01                              → ["node01"]
        node[01-03]                         → ["node01", "node02", "node03"]
        node[01,03,05]                      → ["node01", "node03", "node05"]
        node[01-03,07]                      → ["node01", "node02", "node03", "node07"]
        a-01,a-02,b-[01-03]                → ["a-01", "a-02", "b-01", "b-02", "b-03"]
        a-01,b-[01,03],c-02                → ["a-01", "b-01", "b-03", "c-02"]
    """
    nodes: list[str] = []
    # Split on commas that are NOT inside brackets.
    # We do this by tracking bracket depth.
    segments: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in nodelist:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            segments.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        segments.append("".join(current).strip())

    for segment in segments:
        m = re.match(r"^(.*?)\[([^\]]+)\](.*)$", segment)
        if not m:
            if segment:
                nodes.append(segment)
            continue
        prefix, ranges, suffix = m.group(1), m.group(2), m.group(3)
        for part in ranges.split(","):
            part = part.strip()
            if "-" in part:
                start_s, end_s = part.split("-", 1)
                width = len(start_s)
                for n in range(int(start_s), int(end_s) + 1):
                    nodes.append(f"{prefix}{str(n).zfill(width)}{suffix}")
            else:
                nodes.append(f"{prefix}{part}{suffix}")
    return nodes


# ---------------------------------------------------------------------------
# ZmqTaskServer — coordinator side (ROUTER)
# ---------------------------------------------------------------------------


class ZmqTaskServer:
    """Coordinator-side ZMQ server using a ROUTER socket.

    Workers connect as DEALER sockets and identify themselves by rank on
    their first REGISTER message. After registration the coordinator can
    send targeted messages to any worker by rank.

    Args:
        port: Port to bind on. Workers connect to this same port.
    """

    def __init__(self, port: int = 5555) -> None:
        try:
            import zmq
        except ImportError as e:
            raise ImportError(
                "pyzmq is required for distributed execution. Install it with: pip install pyzmq"
            ) from e

        self._port = port
        self._ctx = zmq.Context()
        self._socket = self._ctx.socket(zmq.ROUTER)
        self._socket.bind(f"tcp://*:{port}")
        self._identities: dict[int, bytes] = {}  # rank → ZMQ identity bytes
        self._buffer: deque[tuple[int, dict]] = deque()

    def send(self, rank: int, msg: dict) -> None:
        """Send a message to a specific worker by rank.

        The worker must have registered first (i.e. its identity must be known).

        Raises:
            KeyError: if the worker rank has not registered yet.
        """
        identity = self._identities[rank]
        self._socket.send_multipart([identity, b"", self._encode(msg)])

    def requeue(self, rank: int, msg: dict) -> None:
        """Push a previously received message back so the next recv() returns it."""
        self._buffer.append((rank, msg))

    def recv(self, timeout_ms: int = 100) -> tuple[int, dict] | None:
        """Receive the next message from any worker.

        Automatically records worker identities from REGISTER messages.

        Returns:
            (rank, msg) tuple, or None if no message arrived within timeout_ms.
        """
        if self._buffer:
            return self._buffer.popleft()
        if not self._socket.poll(timeout_ms):
            return None
        identity, _, raw = self._socket.recv_multipart()
        msg = self._decode(raw)
        rank = int(msg.get("rank", -1))
        if rank >= 0:
            self._identities[rank] = identity
        return rank, msg

    def close(self) -> None:
        self._socket.close()
        self._ctx.term()

    def __enter__(self) -> ZmqTaskServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _encode(msg: dict) -> bytes:
        import json

        return json.dumps(msg).encode()

    @staticmethod
    def _decode(raw: bytes) -> dict:
        import json

        return json.loads(raw.decode())


# ---------------------------------------------------------------------------
# ZmqTaskClient — worker side (DEALER)
# ---------------------------------------------------------------------------


class ZmqTaskClient:
    """Worker-side ZMQ client using a DEALER socket.

    Connects to the coordinator's ROUTER socket. The DEALER identity is set
    to the worker's rank string so the coordinator can correlate messages to
    ranks before the REGISTER handshake completes.

    Args:
        host: Hostname of the coordinator (rank 0).
        port: Port the coordinator is bound on.
        rank: This worker's rank. Used as the socket identity.
    """

    def __init__(self, host: str, port: int = 5555, rank: int = 0) -> None:
        try:
            import zmq
        except ImportError as e:
            raise ImportError(
                "pyzmq is required for distributed execution. Install it with: pip install pyzmq"
            ) from e

        self._rank = rank
        self._ctx = zmq.Context()
        self._socket = self._ctx.socket(zmq.DEALER)
        self._socket.identity = str(rank).encode()
        self._socket.connect(f"tcp://{host}:{port}")

    def send(self, msg: dict) -> None:
        """Send a message to the coordinator."""
        self._socket.send_multipart([b"", self._encode(msg)])

    def recv(self, timeout_ms: int = 100) -> dict | None:
        """Receive the next message from the coordinator.

        Returns:
            Decoded message dict, or None if no message arrived within timeout_ms.
        """
        if not self._socket.poll(timeout_ms):
            return None
        _, raw = self._socket.recv_multipart()
        return self._decode(raw)

    def close(self) -> None:
        self._socket.close()
        self._ctx.term()

    def __enter__(self) -> ZmqTaskClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _encode(msg: dict) -> bytes:
        import json

        return json.dumps(msg).encode()

    @staticmethod
    def _decode(raw: bytes) -> dict:
        import json

        return json.loads(raw.decode())
