"""Isolation and failure-boundary tests for structured REPL imports."""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from servers import lean_project_fingerprint
from servers.repl import core as repl_core
from servers.repl import pool as repl_pool
from servers.repl.imports import (
    ResolvedImports,
    StaleResolvedImportsError,
    resolve_project_imports,
)


def _project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Fixture"\n', encoding="utf-8")
    return project


def _descriptor(project, *modules):
    descriptor = object.__new__(ResolvedImports)
    object.__setattr__(descriptor, "project_root", project.resolve())
    object.__setattr__(descriptor, "modules", tuple(modules))
    object.__setattr__(
        descriptor,
        "project_fingerprint",
        lean_project_fingerprint(project.resolve()),
    )
    return descriptor


def _instrumented_repl(project, monkeypatch, responses):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            cwd=str(project),
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=7,
        )
    )
    events = []
    process_number = {"value": 0}

    def close():
        events.append(("close", process_number["value"]))
        repl.process = None
        repl._project_fingerprint = None

    def start(startup_timeout=None, *, warmup_imports=None):
        process_number["value"] += 1
        events.append(
            (
                "start",
                process_number["value"],
                startup_timeout,
                warmup_imports,
            )
        )
        repl.process = SimpleNamespace(poll=lambda: None)
        repl._project_fingerprint = lean_project_fingerprint(project.resolve())

    def run(code, env_id, timeout):
        events.append(("run", process_number["value"], code, env_id, timeout))
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    repl.process = SimpleNamespace(poll=lambda: None)
    repl._project_fingerprint = lean_project_fingerprint(project.resolve())
    monkeypatch.setattr(repl, "close", close)
    monkeypatch.setattr(repl, "start", start)
    monkeypatch.setattr(repl, "_run", run)
    return repl, events


def test_structured_execution_uses_one_fresh_process_and_returns_no_handles(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture.B", "Fixture.A", "Fixture.B")
    repl, events = _instrumented_repl(
        project,
        monkeypatch,
        [
            {"env": 11, "messages": []},
            {
                "env": 12,
                "messages": [],
                "sorries": [{"goal": "False", "proofState": 13}],
                "tactics": [{"proofState": 14, "text": "exact False.elim"}],
            },
        ],
    )
    freshness_checks = []
    monkeypatch.setattr(
        ResolvedImports,
        "assert_current",
        lambda self, deadline: freshness_checks.append(deadline),
    )
    clock = iter([10.0, 11.0, 12.0])
    monkeypatch.setattr(repl_core.time, "monotonic", lambda: next(clock))

    response = repl.run(
        "example : False := by sorry",
        imports=descriptor,
        deadline=20.0,
    )

    assert response == {
        "messages": [],
        "sorries": [{"goal": "False"}],
        "tactics": [{"text": "exact False.elim"}],
    }
    assert freshness_checks == [20.0] * 4
    assert [event[:2] for event in events] == [
        ("close", 0),
        ("start", 1),
        ("run", 1),
        ("run", 1),
        ("close", 1),
    ]
    assert events[1][3] == ()
    assert events[1][2] == 10.0
    assert events[2][2:4] == (
        "import Fixture.B\nimport Fixture.A\nimport Fixture.B",
        None,
    )
    assert events[3][2:4] == ("example : False := by sorry", 11)
    assert events[2][4] == 9.0
    assert events[3][4] == 8.0
    assert repl.process is None


def test_repeated_structured_requests_never_reuse_process_or_body_state(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture")
    repl, events = _instrumented_repl(
        project,
        monkeypatch,
        [
            {"env": 11, "messages": []},
            {"env": 12, "messages": []},
            {"env": 21, "messages": []},
            {"env": 22, "messages": []},
        ],
    )
    monkeypatch.setattr(ResolvedImports, "assert_current", lambda self, deadline: None)

    repl.run("def RequestOnly : Nat := 1", imports=descriptor, timeout=5)
    repl.run("#check RequestOnly", imports=descriptor, timeout=5)

    assert [event[1] for event in events if event[0] == "start"] == [1, 2]
    assert [event[1] for event in events if event[0] == "run"] == [1, 1, 2, 2]
    assert repl.process is None


@pytest.mark.parametrize(
    "code",
    [
        "import Mathlib\n#check Nat",
        "/- lead -/ import Mathlib\n#check Nat",
        "module Fixture\npublic import Mathlib\n#check Nat",
        "\timport Mathlib\n#check Nat",
        "/-! docs -/\nimport Mathlib\n#check Nat",
        "public\nimport Mathlib\n#check Nat",
    ],
)
def test_structured_execution_rejects_source_headers_before_touching_worker(
    tmp_path, monkeypatch, code
):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture")
    repl = repl_core.LeanRepl(repl_core.LeanReplConfig(cwd=str(project)))
    monkeypatch.setattr(
        ResolvedImports,
        "assert_current",
        lambda *args: pytest.fail("mixed imports must not inspect the descriptor"),
    )
    monkeypatch.setattr(
        repl,
        "close",
        lambda: pytest.fail("mixed imports must not touch the worker"),
    )

    response = repl.run(code, imports=descriptor, timeout=1)

    assert "imports" in response["repl_error"]


def test_structured_import_error_never_dispatches_the_body(tmp_path, monkeypatch):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture")
    import_failure = {
        "env": 11,
        "messages": [
            {
                "severity": "error",
                "data": "unknown module",
                "pos": {"line": 1, "column": 0},
            }
        ],
    }
    repl, events = _instrumented_repl(project, monkeypatch, [import_failure])
    monkeypatch.setattr(ResolvedImports, "assert_current", lambda self, deadline: None)

    response = repl.run("#check Missing", imports=descriptor, timeout=1)

    assert response == {
        "messages": import_failure["messages"],
    }
    assert [event[2] for event in events if event[0] == "run"] == ["import Fixture"]
    assert repl.process is None


def test_stale_imports_before_body_never_dispatch_the_body(tmp_path, monkeypatch):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture")
    repl, events = _instrumented_repl(
        project,
        monkeypatch,
        [{"env": 11, "messages": []}],
    )
    checks = {"count": 0}

    def assert_current(self, deadline):
        checks["count"] += 1
        if checks["count"] == 3:
            raise StaleResolvedImportsError("resolved Lean imports are stale")

    monkeypatch.setattr(ResolvedImports, "assert_current", assert_current)

    response = repl.run("#check Fixture.value", imports=descriptor, timeout=1)

    assert response == {"repl_error": "resolved Lean imports are stale"}
    assert [event[2] for event in events if event[0] == "run"] == ["import Fixture"]
    assert repl.process is None


@pytest.mark.parametrize("body_kind", ["response", "backlog", "explicit-error"])
def test_post_body_freshness_failure_is_always_an_unknown_outcome(
    tmp_path, monkeypatch, body_kind
):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture")
    if body_kind == "response":
        body = {"env": 12, "messages": []}
    elif body_kind == "backlog":
        body = repl_core.ReplStderrBacklog(
            "stderr backlog",
            {"env": 12, "messages": []},
        )
    else:
        body = {"message": "explicit failure"}
    repl, events = _instrumented_repl(
        project,
        monkeypatch,
        [{"env": 11, "messages": []}, body],
    )
    checks = {"count": 0}

    def assert_current(self, deadline):
        checks["count"] += 1
        if checks["count"] == 4:
            raise StaleResolvedImportsError("resolved Lean imports are stale")

    monkeypatch.setattr(ResolvedImports, "assert_current", assert_current)

    response = repl.run("#eval sideEffect", imports=descriptor, timeout=1)

    assert response["outcome_unknown"] is True
    assert "freshness changed" in response["repl_error"]
    assert len([event for event in events if event[0] == "run"]) == 2
    assert repl.process is None


@pytest.mark.parametrize("body_kind", ["backlog", "explicit-error"])
def test_terminal_body_responses_are_checked_then_sanitized(
    tmp_path, monkeypatch, body_kind
):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture")
    if body_kind == "backlog":
        body = repl_core.ReplStderrBacklog(
            "stderr backlog",
            {
                "env": 12,
                "messages": [],
                "sorries": [{"goal": "False", "proofState": 13}],
            },
        )
        expected = {"messages": [], "sorries": [{"goal": "False"}]}
    else:
        body = {"message": "explicit failure"}
        expected = {"repl_error": "explicit failure"}
    repl, _ = _instrumented_repl(
        project,
        monkeypatch,
        [{"env": 11, "messages": []}, body],
    )
    checks = []
    monkeypatch.setattr(
        ResolvedImports,
        "assert_current",
        lambda self, deadline: checks.append(deadline),
    )

    response = repl.run("#check Fixture.value", imports=descriptor, timeout=1)

    assert response == expected
    assert len(checks) == 4
    assert repl.process is None


def test_structured_import_transport_failure_is_not_retried(tmp_path, monkeypatch):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture")
    repl, events = _instrumented_repl(
        project,
        monkeypatch,
        [repl_core.ReplProcessExited("closed before send")],
    )
    monkeypatch.setattr(ResolvedImports, "assert_current", lambda self, deadline: None)

    response = repl.run("#check Fixture.value", imports=descriptor, timeout=1)

    assert response == {"repl_error": "closed before send"}
    assert len([event for event in events if event[0] == "run"]) == 1
    assert len([event for event in events if event[0] == "start"]) == 1
    assert repl.process is None


def test_structured_body_transport_failure_is_not_retried(tmp_path, monkeypatch):
    project = _project(tmp_path)
    descriptor = _descriptor(project, "Fixture")
    repl, events = _instrumented_repl(
        project,
        monkeypatch,
        [
            {"env": 11, "messages": []},
            repl_core.ReplOutcomeUnknown("body transport failed after dispatch"),
        ],
    )
    monkeypatch.setattr(ResolvedImports, "assert_current", lambda self, deadline: None)

    response = repl.run("#eval sideEffect", imports=descriptor, timeout=1)

    assert response == {
        "repl_error": "body transport failed after dispatch",
        "outcome_unknown": True,
    }
    assert len([event for event in events if event[0] == "run"]) == 2
    assert len([event for event in events if event[0] == "start"]) == 1
    assert repl.process is None


def test_structured_imports_reject_wrong_root_and_raw_environment(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "lakefile.toml").write_text('name = "Other"\n', encoding="utf-8")
    descriptor = _descriptor(other, "Fixture")
    repl = repl_core.LeanRepl(repl_core.LeanReplConfig(cwd=str(project)))
    monkeypatch.setattr(
        repl,
        "close",
        lambda: pytest.fail("invalid imports must not touch the worker"),
    )

    wrong_root = repl.run("#check Nat", imports=descriptor, timeout=1)
    explicit_environment = repl.run(
        "#check Nat",
        env_id=11,
        imports=descriptor,
        timeout=1,
    )

    assert "different Lean project root" in wrong_root["repl_error"]
    assert "explicit environment" in explicit_environment["repl_error"]


def test_structured_imports_require_a_resolver_issued_descriptor(tmp_path):
    project = _project(tmp_path)
    repl = repl_core.LeanRepl(repl_core.LeanReplConfig(cwd=str(project)))

    with pytest.raises(TypeError, match="ResolvedImports"):
        repl.run("#check Nat", imports=object(), timeout=1)


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_repl_execution_is_disposable_across_structured_and_plain_calls(tmp_path):
    project = tmp_path / "fixture"
    project.mkdir()
    (project / "lean-toolchain").write_text(
        "leanprover/lean4:v4.32.2\n",
        encoding="utf-8",
    )
    (project / "lakefile.toml").write_text(
        '''name = "Fixture"
version = "0.1.0"
defaultTargets = ["Fixture"]

[[require]]
name = "repl"
git = "https://github.com/leanprover-community/repl.git"
rev = "68a3b3a059787a7db44fb1e6281e4a657efee470"

[[lean_lib]]
name = "Fixture"
srcDir = "src"
''',
        encoding="utf-8",
    )
    source = project / "src" / "Fixture.lean"
    source.parent.mkdir()
    source.write_text(
        "namespace Fixture\n\ndef localValue : Nat := 37\n\nend Fixture\n",
        encoding="utf-8",
    )
    for command in (["lake", "update"], ["lake", "build", "Fixture", "repl"]):
        result = subprocess.run(
            command,
            cwd=project,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    descriptor = resolve_project_imports(project, ("Fixture",), timeout=60)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(
            cwd=str(project),
            num_repls=1,
            startup_stagger=0,
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=0,
        )
    )
    worker = pool._workers[0]
    starts = []
    original_start = worker.start

    def tracked_start(*args, **kwargs):
        original_start(*args, **kwargs)
        starts.append(worker.process.pid)

    worker.start = tracked_start
    try:
        first = pool.run(
            "#check Fixture.localValue\ndef RequestOnly : Nat := Fixture.localValue",
            imports=descriptor,
            timeout=120,
        )
        assert worker.process is None
        second = pool.run(
            "#check Fixture.localValue\n#check RequestOnly",
            imports=descriptor,
            timeout=120,
        )
        assert worker.process is None
        plain_first = pool.run(
            "def PlainOnly : Nat := 41\n#check PlainOnly",
            timeout=120,
        )
        assert worker.process is None
        plain_second = pool.run("#check PlainOnly", timeout=120)
        assert worker.process is None
    finally:
        pool.shutdown()

    assert len(starts) == 4
    assert "env" not in first
    assert "env" not in second
    assert "env" not in plain_first
    assert "env" not in plain_second
    assert first["messages"][0]["data"] == "Fixture.localValue : Nat"
    assert second["messages"][0]["data"] == "Fixture.localValue : Nat"
    assert second["messages"][1]["severity"] == "error"
    assert "Unknown identifier `RequestOnly`" in second["messages"][1]["data"]
    assert plain_first["messages"][0]["data"] == "PlainOnly : Nat"
    assert plain_second["messages"][0]["severity"] == "error"
    assert "Unknown identifier `PlainOnly`" in plain_second["messages"][0]["data"]
