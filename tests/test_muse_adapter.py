from __future__ import annotations

import json
import shutil

import pytest

from servers.prover._cli_common import ProverTimeout
from servers.prover.base import EventKind, SteeringCapability
from servers.prover.muse_adapter import (
    MuseAdapter,
    classify_muse_event,
    parse_muse_terminal_output,
)


def _record(payload_type: str, payload: dict, sequence: int = 1) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "stream": {"kind": "session", "id": "session-1"},
            "sequence": sequence,
            "payload_type": payload_type,
            "payload": payload,
        }
    )


def test_muse_event_parser_only_treats_run_terminal_as_failure():
    task_failure = json.loads(
        _record(
            "task.lifecycle.failed",
            {"event": {"kind": "failed", "reason": "optional reminder failed"}},
        )
    )
    event, final, error, session_id, usage = classify_muse_event(task_failure)
    assert event is None
    assert final is None
    assert error is None
    assert session_id == "session-1"
    assert usage == {}

    terminal = json.loads(
        _record("run.terminal.failed", {"reason": "provider unavailable"})
    )
    event, final, error, _session_id, _usage = classify_muse_event(terminal)
    assert event is not None and event.kind is EventKind.ERROR
    assert final is None
    assert error == "provider unavailable"


def test_parse_muse_terminal_output_prefers_terminal_text_and_collects_usage():
    stdout = "\n".join(
        [
            _record("run.output.delta", {"text": "partial"}, 1),
            _record(
                "run.terminal.completed",
                {
                    "text": '{"score":5,"reasoning":"checked"}',
                    "usage": {"input_tokens": 8, "output_tokens": 3},
                },
                2,
            ),
        ]
    )
    final, error, usage = parse_muse_terminal_output(stdout)
    assert final == '{"score":5,"reasoning":"checked"}'
    assert error == ""
    assert usage == {"input_tokens": 8, "output_tokens": 3}


def test_muse_adapter_runs_with_sandbox_and_isolated_plugin_data(tmp_path):
    captured = {}

    def runner(args, env, cwd, deadline):
        captured.update(args=args, env=env, cwd=cwd, deadline=deadline)
        yield _record(
            "task.lifecycle.failed",
            {"event": {"kind": "failed", "reason": "optional reminder failed"}},
            1,
        )
        yield _record("run.output.delta", {"text": "working"}, 2)
        yield _record("run.terminal.completed", {"text": "proof complete"}, 3)

    runtime = tmp_path / "muse-runtime"
    adapter = MuseAdapter(
        muse_bin="tbh-test",
        model="muse-test",
        runtime_dir=str(runtime),
        max_wait_seconds=10,
        runner=runner,
    )
    run = adapter.start("Target", "theorem Target : True", str(tmp_path))
    events = list(adapter.events(run))
    result = adapter.result(run)

    assert adapter.steering is SteeringCapability.NONE
    assert result.status == "proved"
    assert result.backend == "muse"
    assert result.meta["session_id"] == "session-1"
    assert any(event.kind is EventKind.RESULT for event in events)
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["XDG_DATA_HOME"] == str(runtime.resolve())
    args = captured["args"]
    assert args[:3] == ["tbh-test", "exec", "--json"]
    assert ["--workspace", str(tmp_path)] == args[
        args.index("--workspace") : args.index("--workspace") + 2
    ]
    assert "--disable-approval" in args
    assert "--no-foreign-personal-context" in args
    assert "--no-session-log" in args
    assert "--yolo" not in args
    assert "--disable-sandbox" not in args


def test_muse_adapter_terminal_failure_and_timeout_fail_closed(tmp_path):
    def failed_runner(args, env, cwd, deadline):
        yield _record("run.terminal.failed", {"reason": "provider rejected run"})

    failed = MuseAdapter(runtime_dir=str(tmp_path / "failed"), runner=failed_runner)
    run = failed.start("Target", "spec", str(tmp_path))
    list(failed.events(run))
    result = failed.result(run)
    assert result.status == "failed"
    assert result.reason == "provider rejected run"
    assert result.meta["sub_status"] == "transport"

    def timeout_runner(args, env, cwd, deadline):
        raise ProverTimeout("expired")

    timed = MuseAdapter(
        runtime_dir=str(tmp_path / "timed"),
        max_wait_seconds=1,
        runner=timeout_runner,
    )
    run = timed.start("Target", "spec", str(tmp_path))
    events = list(timed.events(run))
    result = timed.result(run)
    assert events[-1].kind is EventKind.ERROR
    assert result.status == "failed"
    assert result.meta["sub_status"] == "timeout"


def test_muse_server_factory_registers_adapter():
    from servers.prover.server import _make_adapter

    adapter = _make_adapter("muse", "graph.json", 30)
    assert isinstance(adapter, MuseAdapter)
    assert adapter.steering is SteeringCapability.NONE


@pytest.mark.skipif(shutil.which("tbh") is None, reason="Muse/TBH CLI is not installed")
def test_live_muse_echo_jsonl_contract(tmp_path):
    adapter = MuseAdapter(
        provider="echo",
        max_model_steps=1,
        runtime_dir=str(tmp_path / "runtime"),
        max_wait_seconds=30,
    )
    run = adapter.start("Echo", "Report the target", str(tmp_path))
    events = list(adapter.events(run))
    result = adapter.result(run)
    assert result.status == "proved"
    assert result.meta["session_id"]
    assert any(event.kind is EventKind.RESULT for event in events)
    assert "echo:" in result.proof_text
