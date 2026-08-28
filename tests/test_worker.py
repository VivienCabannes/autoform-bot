"""Tests for the cron-facing unattended Codex reconciler."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from autoform_cli import worker


_FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

calls = Path(os.environ["FAKE_CODEX_CALLS"])
with calls.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\n")

result = Path(sys.argv[sys.argv.index("-o") + 1])
resume = "resume" in sys.argv[1:]
mode = os.environ.get("FAKE_CODEX_MODE", "continue_complete")
print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)

if mode == "sleep":
    time.sleep(60)
elif mode == "tool_hang":
    item = {"id": "tool-1", "type": "command_execution", "command": "lake build"}
    print(json.dumps({"type": "item.started", "item": item}), flush=True)
    time.sleep(60)
elif mode == "retry_hang":
    print(json.dumps({"type": "error", "message": "API error, retrying"}), flush=True)
    print(json.dumps({"type": "error", "message": "connection retry"}), flush=True)
    print("API error, retry", file=sys.stderr, flush=True)
    time.sleep(60)
else:
    status = "complete" if resume else "continue"
    payload = {
        "status": status,
        "summary": "fake turn finished",
        "next_action": "resume once" if status == "continue" else "none",
    }
    result.write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps({"type": "turn.completed"}), flush=True)
    time.sleep(0.1)
'''


def _make_repository(
    tmp_path: Path,
    *,
    stale_minutes: float = 15,
    tool_minutes: float = 60,
    max_restarts: int = 3,
) -> Path:
    repository = tmp_path / "formalization"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / ".git" / "info" / "exclude").write_text(".aiworker\n", encoding="utf-8")
    (repository / ".aiworker").write_text(
        "\n".join(
            [
                "version = 1",
                'objective = "Continue the approved roadmap."',
                f"stale_after_minutes = {stale_minutes}",
                f"tool_stale_after_minutes = {tool_minutes}",
                f"max_restarts_per_hour = {max_restarts}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return repository


def _make_fake_codex(tmp_path: Path, monkeypatch, mode: str) -> tuple[Path, Path]:
    executable = tmp_path / "fake-codex"
    executable.write_text(_FAKE_CODEX, encoding="utf-8")
    executable.chmod(0o755)
    calls = tmp_path / "codex-calls.jsonl"
    monkeypatch.setenv("FAKE_CODEX_CALLS", str(calls))
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    return executable, calls


def _state(repository: Path, state_home: Path) -> tuple[worker.WorkerPaths, dict]:
    paths = worker.WorkerPaths.for_repository(repository.resolve(), state_home)
    return paths, json.loads(paths.state.read_text(encoding="utf-8"))


def _wait_until_stopped(repository: Path, state_home: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, state = _state(repository, state_home)
        if not worker._owned_process(state):
            return
        time.sleep(0.025)
    raise AssertionError("fake Codex did not stop")


def _wait_for_text(path: Path, needle: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text(encoding="utf-8"):
            return
        time.sleep(0.025)
    raise AssertionError(f"{needle!r} did not appear in {path}")


def _stop_test_worker(repository: Path, state_home: Path) -> None:
    try:
        _, state = _state(repository, state_home)
    except FileNotFoundError:
        return
    worker._terminate_owned_process(state)
    worker._reap_child(state.get("pid", 0))


def test_worker_starts_once_and_leaves_a_healthy_process_alone(tmp_path, monkeypatch, capsys) -> None:
    repository = _make_repository(tmp_path)
    executable, _ = _make_fake_codex(tmp_path, monkeypatch, "sleep")
    state_home = tmp_path / "state"
    try:
        assert worker.reconcile(
            repository,
            now=100,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
        paths, first = _state(repository, state_home)
        _wait_for_text(paths.run_files(1)[0], "thread.started")

        assert worker.reconcile(
            repository,
            now=101,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
        _, second = _state(repository, state_home)
        assert second["pid"] == first["pid"]
        assert second["generation"] == 1
        assert "healthy pid" in capsys.readouterr().out
    finally:
        _stop_test_worker(repository, state_home)


def test_worker_resumes_continue_result_then_stops_on_complete(tmp_path, monkeypatch, capsys) -> None:
    repository = _make_repository(tmp_path)
    executable, calls_path = _make_fake_codex(tmp_path, monkeypatch, "continue_complete")
    state_home = tmp_path / "state"

    assert worker.reconcile(
        repository,
        now=100,
        state_home=state_home,
        codex_bin=str(executable),
        require_linux=False,
    ) == 0
    _wait_until_stopped(repository, state_home)
    assert worker.reconcile(
        repository,
        now=101,
        state_home=state_home,
        codex_bin=str(executable),
        require_linux=False,
    ) == 0
    _, resumed = _state(repository, state_home)
    assert resumed["generation"] == 2
    assert resumed["session_id"] == "fake-thread"

    _wait_until_stopped(repository, state_home)
    assert worker.reconcile(
        repository,
        now=102,
        state_home=state_home,
        codex_bin=str(executable),
        require_linux=False,
    ) == 0
    _, finished = _state(repository, state_home)
    assert finished["status"] == "complete"
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    assert "resume" not in calls[0]
    assert "resume" in calls[1]
    assert "fake-thread" in calls[1]
    assert "complete: fake turn finished" in capsys.readouterr().out


def test_worker_uses_the_longer_timeout_for_an_active_tool(tmp_path, monkeypatch) -> None:
    repository = _make_repository(tmp_path, stale_minutes=0.01, tool_minutes=0.1)
    executable, _ = _make_fake_codex(tmp_path, monkeypatch, "tool_hang")
    state_home = tmp_path / "state"
    try:
        assert worker.reconcile(
            repository,
            now=100,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
        paths, _ = _state(repository, state_home)
        _wait_for_text(paths.run_files(1)[0], "command_execution")
        assert worker.reconcile(
            repository,
            now=101,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
        assert worker.reconcile(
            repository,
            now=104,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
        _, state = _state(repository, state_home)
        assert state["active_long_items"] == ["tool-1"]
        assert state["stale_observations"] == 0
        assert worker._owned_process(state)
    finally:
        _stop_test_worker(repository, state_home)


def test_removing_configuration_stops_worker_and_recreating_it_starts_fresh(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repository = _make_repository(tmp_path)
    executable, _ = _make_fake_codex(tmp_path, monkeypatch, "sleep")
    state_home = tmp_path / "state"
    config_text = (repository / ".aiworker").read_text(encoding="utf-8")
    try:
        assert worker.reconcile(
            repository,
            now=100,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
        (repository / ".aiworker").unlink()
        assert worker.reconcile(
            repository,
            now=101,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
        _, disabled = _state(repository, state_home)
        assert disabled["status"] == "disabled"
        assert not worker._owned_process(disabled)

        (repository / ".aiworker").write_text(config_text, encoding="utf-8")
        assert worker.reconcile(
            repository,
            now=102,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
        _, restarted = _state(repository, state_home)
        assert restarted["status"] == "running"
        assert restarted["generation"] == 2
        assert restarted["session_id"] is None
        output = capsys.readouterr().out
        assert "disabled" in output
        assert "configuration restored" in output
    finally:
        _stop_test_worker(repository, state_home)


def test_worker_confirms_retry_stall_before_killing_and_opens_circuit(tmp_path, monkeypatch, capsys) -> None:
    repository = _make_repository(
        tmp_path,
        stale_minutes=0.01,
        tool_minutes=0.02,
        max_restarts=1,
    )
    executable, _ = _make_fake_codex(tmp_path, monkeypatch, "retry_hang")
    state_home = tmp_path / "state"

    assert worker.reconcile(
        repository,
        now=100,
        state_home=state_home,
        codex_bin=str(executable),
        require_linux=False,
    ) == 0
    paths, running = _state(repository, state_home)
    _wait_for_text(paths.run_files(1)[0], "API error")

    assert worker.reconcile(
        repository,
        now=101,
        state_home=state_home,
        codex_bin=str(executable),
        require_linux=False,
    ) == 0
    assert worker.reconcile(
        repository,
        now=102,
        state_home=state_home,
        codex_bin=str(executable),
        require_linux=False,
    ) == 0
    _, suspected = _state(repository, state_home)
    assert suspected["pid"] == running["pid"]
    assert suspected["stale_observations"] == 1

    assert worker.reconcile(
        repository,
        now=103,
        state_home=state_home,
        codex_bin=str(executable),
        require_linux=False,
    ) == 0
    _, waiting = _state(repository, state_home)
    assert waiting["status"] == "waiting"
    assert waiting["not_before"] == 163
    assert waiting["retry_errors"] >= 2
    assert not worker._owned_process(waiting)

    assert worker.reconcile(
        repository,
        now=164,
        state_home=state_home,
        codex_bin=str(executable),
        require_linux=False,
    ) == 0
    _wait_for_text(paths.run_files(2)[0], "API error")
    for current_time in (165, 166, 167):
        assert worker.reconcile(
            repository,
            now=current_time,
            state_home=state_home,
            codex_bin=str(executable),
            require_linux=False,
        ) == 0
    _, circuit = _state(repository, state_home)
    assert circuit["status"] == "waiting"
    assert circuit["not_before"] == 3703
    assert not worker._owned_process(circuit)
    output = capsys.readouterr().out
    assert "possible stall" in output
    assert "restart circuit open" in output


def test_worker_refuses_a_tracked_configuration(tmp_path, monkeypatch, capsys) -> None:
    repository = _make_repository(tmp_path)
    executable, _ = _make_fake_codex(tmp_path, monkeypatch, "sleep")
    subprocess.run(["git", "-C", str(repository), "add", "-f", ".aiworker"], check=True)

    assert worker.reconcile(
        repository,
        state_home=tmp_path / "state",
        codex_bin=str(executable),
        require_linux=False,
    ) == 2
    assert "must not be tracked by Git" in capsys.readouterr().err


def test_worker_lock_is_nonblocking(tmp_path) -> None:
    lock = tmp_path / "worker.lock"
    with worker._state_lock(lock) as first:
        assert first
        with worker._state_lock(lock) as second:
            assert not second


def test_worker_rejects_a_reused_pid_identity(monkeypatch) -> None:
    class ReusedProcess:
        def status(self):
            return "running"

        def is_running(self):
            return True

        def create_time(self):
            return 200.0

    monkeypatch.setattr(worker.psutil, "Process", lambda _pid: ReusedProcess())
    monkeypatch.setattr(worker.os, "getpgid", lambda pid: pid)
    state = {
        "pid": 42,
        "process_created_at": 100.0,
        "process_group": 42,
    }
    assert not worker._owned_process(state)
