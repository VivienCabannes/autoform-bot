# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for LeanReplServer.close() — counter-aware shutdown logic."""

from pathlib import Path
from unittest.mock import MagicMock

from .server import LeanReplServer, LeanReplServerArgs


def _make_server(tmp_path: Path, *, counter: int | None = None) -> LeanReplServer:
    """Create a LeanReplServer with temp coordination files (no real REPL)."""
    args = LeanReplServerArgs(dump_dir=str(tmp_path), cwd=".", repl_command=["true"])
    server = LeanReplServer(args)

    # Write coordination files as if the server were already running.
    Path(server._server_addr_file).parent.mkdir(parents=True, exist_ok=True)
    Path(server._server_addr_file).write_text("http://localhost:9999")
    if counter is not None:
        Path(server._client_counter_file).write_text(str(counter))

    return server


def test_close_decrements_counter(tmp_path: Path):
    """Closing with counter=2 decrements to 1 and keeps files."""
    server = _make_server(tmp_path, counter=2)

    server.close()

    assert Path(server._client_counter_file).read_text() == "1"
    assert Path(server._server_addr_file).exists()


def test_close_last_client_cleans_up(tmp_path: Path):
    """Closing as the last client (counter=1) removes coordination files."""
    server = _make_server(tmp_path, counter=1)

    server.close()

    assert not Path(server._client_counter_file).exists()
    assert not Path(server._server_addr_file).exists()


def test_close_with_pool_shuts_down_pool(tmp_path: Path):
    """Last client with an owned pool calls pool.shutdown()."""
    server = _make_server(tmp_path, counter=1)
    server.pool = MagicMock()

    server.close()

    server.pool is None or server.pool.shutdown.assert_called_once()
    assert not Path(server._client_counter_file).exists()


def test_close_nonzero_counter_preserves_pool(tmp_path: Path):
    """Pool is NOT shut down when other clients remain."""
    server = _make_server(tmp_path, counter=2)
    pool = MagicMock()
    server.pool = pool

    server.close()

    pool.shutdown.assert_not_called()
    assert server.pool is pool


def test_close_missing_counter_file(tmp_path: Path):
    """Missing counter file: shuts down pool if owned, no crash."""
    args = LeanReplServerArgs(dump_dir=str(tmp_path), cwd=".", repl_command=["true"])
    server = LeanReplServer(args)
    Path(server._server_addr_file).parent.mkdir(parents=True, exist_ok=True)
    # No counter file written.
    pool = MagicMock()
    server.pool = pool

    server.close()

    pool.shutdown.assert_called_once()
    assert server.pool is None


def test_close_clamps_counter_to_zero(tmp_path: Path):
    """Counter already at 0: clamps, cleans up, no negative."""
    server = _make_server(tmp_path, counter=0)

    server.close()

    assert not Path(server._client_counter_file).exists()
    assert not Path(server._server_addr_file).exists()


def test_close_double_call_safe(tmp_path: Path):
    """Calling close() twice does not crash."""
    server = _make_server(tmp_path, counter=1)
    pool = MagicMock()
    server.pool = pool

    server.close()
    server.close()  # second call — counter file is gone

    pool.shutdown.assert_called_once()
