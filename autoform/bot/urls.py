# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Consolidated URL registry for pipeline services.

All service URLs (registry, control) are stored in a single ``urls.json``
file in the run directory. Uses POSIX record locks (fcntl.lockf) for
NFS-safe concurrent writes from multiple nodes.
"""

from __future__ import annotations

import fcntl
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FILENAME = "urls.json"


def register_url(run_path: Path, kind: str, rank: int, url: str) -> None:
    """Register a service URL.

    Args:
        run_path: Root directory of the run.
        kind: Service type (e.g. ``"registry"``, ``"control"``).
        rank: Rank number that owns this service.
        url: The HTTP URL of the service.
    """
    path = run_path / _FILENAME
    lock_path = run_path / (_FILENAME + ".lock")
    with open(lock_path, "w") as lock:
        fcntl.lockf(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            data.setdefault(kind, {})[str(rank)] = url
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n")
            tmp.rename(path)
        finally:
            fcntl.lockf(lock, fcntl.LOCK_UN)
    logger.info("Registered %s URL for rank %d: %s", kind, rank, url)


def get_urls(run_path: Path, kind: str) -> dict[int, str]:
    """Read all URLs for a service type.

    Returns:
        Mapping from rank number to URL.
    """
    path = run_path / _FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {int(k): v for k, v in data.get(kind, {}).items()}
    except (json.JSONDecodeError, OSError):
        return {}


def cleanup_urls(run_path: Path) -> None:
    """Delete the URLs file."""
    path = run_path / _FILENAME
    try:
        path.unlink(missing_ok=True)
        logger.info("Cleaned up %s", path)
    except OSError:
        pass
