"""Project-scoped Lean import resolution contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from servers import lean_project_fingerprint
from servers.repl import imports as repl_imports
from servers.repl.imports import (
    LeanImportError,
    LeanImportHeaderError,
    ResolvedImports,
    clean_lake_environment,
    resolve_project_imports,
    split_imports_and_body,
    validate_imports,
)

_REAL_QUERY_TRANSITIVE_MODULES = repl_imports._query_transitive_modules
_TEST_MODULE_ROOTS: list[Path] = []

def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Fixture"\n', encoding="utf-8")
    (project / "lake-manifest.json").write_text('{"version": "1.1.0", "packages": []}\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    return project


def _lake_environment(*roots: Path) -> bytes:
    artifact_roots = os.pathsep.join(str(root / "artifacts") for root in roots)
    source_roots = os.pathsep.join(str(root / "sources") for root in roots)
    return f"LEAN_PATH={artifact_roots}\nLEAN_SRC_PATH={source_roots}\n".encode()


def _fake_lake_stdout(command: list[str], *roots: Path) -> bytes:
    if command[-1] == "env":
        return _lake_environment(*roots)
    return b""


@pytest.fixture(autouse=True)
def _stub_lake_transitive_import_query(request, monkeypatch):
    _TEST_MODULE_ROOTS.clear()
    if request.node.get_closest_marker("real_lean") is not None:
        yield
        _TEST_MODULE_ROOTS.clear()
        return

    def query(project_root, modules, deadline):
        pending = list(modules)
        closure = []
        while pending:
            module = pending.pop(0)
            if module in closure:
                continue
            closure.append(module)
            relative = Path(*module.split(".")).with_suffix(".lean")
            source = next(
                (
                    root / "sources" / relative
                    for root in _TEST_MODULE_ROOTS
                    if (root / "sources" / relative).is_file()
                ),
                None,
            )
            if source is None:
                continue
            for line in source.read_text(encoding="utf-8").splitlines():
                if line.startswith("import "):
                    pending.append(line.removeprefix("import ").strip())
        return tuple(closure)

    monkeypatch.setattr(repl_imports, "_query_transitive_modules", query)
    yield
    _TEST_MODULE_ROOTS.clear()


def _module(
    root: Path,
    name: str,
    *,
    source_time: int = 1,
    artifact_time: int = 2,
    imports: tuple[str, ...] = (),
) -> None:
    if root not in _TEST_MODULE_ROOTS:
        _TEST_MODULE_ROOTS.append(root)
    relative = Path(*name.split("."))
    source = root / "sources" / relative.with_suffix(".lean")
    artifact = root / "artifacts" / relative.with_suffix(".olean")
    artifact_hash = artifact.with_suffix(".olean.hash")
    source.parent.mkdir(parents=True, exist_ok=True)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "".join(f"import {module}\n" for module in imports)
        + "theorem fixture : True := by trivial\n"
    )
    artifact.write_bytes(b"olean")
    artifact_hash.write_bytes(b"hash")
    artifact.with_suffix(".trace").write_text(
        '{"schemaVersion":"test","depHash":"fixture","outputs":{}}',
        encoding="utf-8",
    )
    os.utime(source, ns=(source_time, source_time))
    os.utime(artifact, ns=(artifact_time, artifact_time))


def test_structured_import_validation_preserves_order_and_duplicates():
    assert validate_imports(["Fixture.B", "Fixture.A", "Fixture.B"]) == (
        "Fixture.B",
        "Fixture.A",
        "Fixture.B",
    )


@pytest.mark.parametrize(
    "value",
    ["Fixture", [""], [1], ["import Fixture"], ["Fixture/Outside"], ["Fixture..A"]],
)
def test_structured_import_validation_rejects_malformed_values(value):
    with pytest.raises(LeanImportError, match="imports"):
        validate_imports(value)


@pytest.mark.parametrize(
    ("code", "imports", "body", "line_count"),
    [
        ("import Mathlib\n#check Nat", ["Mathlib"], "#check Nat", 1),
        (
            "  /- lead -/ import /- gap -/ Mathlib -- tail\r\n#check Nat",
            ["Mathlib"],
            "#check Nat",
            1,
        ),
        (
            "-- lead\n/- outer\n /- nested -/\n-/\nimport Mathlib\n\n-- body\n#check Nat",
            ["Mathlib"],
            "\n-- body\n#check Nat",
            5,
        ),
        (
            "import Mathlib\n\nimport Aesop\n-- body\n#check Nat",
            ["Mathlib", "Aesop"],
            "-- body\n#check Nat",
            3,
        ),
    ],
)
def test_source_import_scanner_accepts_only_plain_physical_headers(
    code, imports, body, line_count
):
    assert split_imports_and_body(code) == (imports, body, line_count)


@pytest.mark.parametrize(
    "code",
    [
        "import\nMathlib",
        "import -- continued\nMathlib",
        "import /- continued\n-/ Mathlib",
        "module Fixture",
        "prelude",
        "public import Mathlib",
        "meta import Mathlib",
        "import all Mathlib",
        "import mathlib",
        "\timport Mathlib",
        "\N{NO-BREAK SPACE}import Mathlib",
        "import Mathlib Aesop",
        "import Mathlib #check Nat",
        "import Math/- gap -/lib",
        "import Mathlib\r#check Nat",
        "\rimport Mathlib\n#check Nat",
        "-- lead\rimport Mathlib\n#check Nat",
        "/- lead -/\rimport Mathlib\n#check Nat",
        "/- lead\r-/\nimport Mathlib\n#check Nat",
    ],
)
def test_source_import_scanner_rejects_unsupported_headers(code):
    with pytest.raises(LeanImportHeaderError, match="pass module names with imports"):
        split_imports_and_body(code)


@pytest.mark.parametrize(
    "code",
    [
        "/-- docs -/\nimport Mathlib",
        "/-! docs -/\nimport Mathlib",
        "#check Nat\nimport Mathlib",
        "im/- gap -/port Mathlib",
        "meta def fixture : Nat := 1",
        "public def fixture : Nat := 1",
        "\r#check Nat",
    ],
)
def test_source_import_scanner_preserves_non_header_source(code):
    assert split_imports_and_body(code) == ([], code, 0)


def test_source_import_scanner_preserves_unterminated_trailing_comment():
    code = "import Mathlib /- unterminated"
    assert split_imports_and_body(code) == (["Mathlib"], code, 0)


def test_source_import_scanner_preserves_bare_cr_after_the_final_import():
    code = "import Mathlib\n\r#check Nat"
    assert split_imports_and_body(code) == (["Mathlib"], "\r#check Nat", 1)


def test_resolver_rejects_malformed_module_before_running_lake(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("malformed modules must not reach Lake"),
    )

    with pytest.raises(LeanImportError, match="imports"):
        resolve_project_imports(project, ("../Outside",), timeout=1)


def test_resolver_rejects_a_symlink_loop_project_root(tmp_path):
    loop = tmp_path / "project-loop"
    loop.symlink_to(loop, target_is_directory=True)

    with pytest.raises(LeanImportError, match="invalid Lean project root"):
        resolve_project_imports(loop, (), timeout=1)


def test_resolved_imports_cannot_be_constructed_without_resolution(tmp_path):
    with pytest.raises(TypeError):
        ResolvedImports()

    with pytest.raises(TypeError):
        ResolvedImports(
            project_root=tmp_path,
            modules=("Fixture",),
            project_fingerprint=lean_project_fingerprint(tmp_path),
        )

    assert not hasattr(ResolvedImports, "_create")

def test_clean_lake_environment_removes_ambient_path_overrides(monkeypatch):
    for name in (
        "ELAN_TOOLCHAIN",
        "LAKE_CONFIG",
        "LAKE_HOME",
        "LAKE_PKG_URL_MAP",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LEAN_SYSROOT",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "host-value")
    monkeypatch.setenv("HOME", "/safe-home")

    environment = clean_lake_environment()

    assert environment["HOME"] == "/safe-home"
    assert all(environment.get(name) is None for name in (
        "ELAN_TOOLCHAIN",
        "LAKE_HOME",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LEAN_SYSROOT",
        "PYTHONPATH",
    ))


def test_resolver_environment_scrubs_all_ambient_git_controls(monkeypatch):
    poisoned = {
        "GIT_DIR": "/redirect/repository",
        "GIT_WORK_TREE": "/redirect/worktree",
        "GIT_INDEX_FILE": "/redirect/index",
        "GIT_OBJECT_DIRECTORY": "/redirect/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/redirect/alternate-objects",
        "GIT_CONFIG_SYSTEM": "/redirect/system-config",
        "GIT_CONFIG_GLOBAL": "/redirect/global-config",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "include.path",
        "GIT_CONFIG_VALUE_0": "/redirect/injected-config",
        "GIT_FUTURE_CONTROL": "must-also-be-removed",
        "GITHUB_TOKEN": "preserved-non-git-prefix",
        "HOME": "/safe-home",
    }
    monkeypatch.setattr(
        repl_imports,
        "clean_lake_environment",
        lambda: dict(poisoned),
    )

    environment = repl_imports._clean_resolver_environment()

    assert not any(name.startswith("GIT_") for name in environment)
    assert environment["GITHUB_TOKEN"] == "preserved-non-git-prefix"
    assert environment["HOME"] == "/safe-home"


def test_resolver_uses_non_building_lake_environment_and_preserves_order(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture.B")
    _module(root, "Fixture.A")
    calls = []

    def run(command, project_root, *, deadline, **kwargs):
        calls.append((command, project_root, deadline))
        stdout = _fake_lake_stdout(command, root)
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    started = time.monotonic()
    resolved = resolve_project_imports(
        project,
        ("Fixture.B", "Fixture.A", "Fixture.B"),
        timeout=1,
    )

    assert resolved.project_root == project.resolve()
    assert resolved.modules == ("Fixture.B", "Fixture.A", "Fixture.B")
    assert resolved.project_fingerprint == lean_project_fingerprint(project.resolve())
    assert tuple(selection.module for selection in resolved._selections) == (
        "Fixture.B",
        "Fixture.A",
    )
    assert calls[0][0] == ["lake", "--no-build", "env"]
    assert calls[0][1] == project
    assert 0 < calls[0][2] - started <= 1.01
    assert calls[1][2] == calls[0][2]
    assert calls[1][0] == [
        "lake",
        "--rehash",
        "--no-build",
        "build",
        "+Fixture.B:olean",
        "+Fixture.A:olean",
    ]
    assert len(calls) == 2


def test_pinned_clean_git_dependency_is_rehashed_by_lake(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    package = project / ".lake" / "packages" / "fixture_dep"
    _module(package, "Fixture")
    (package / "lakefile.lean").write_text("package fixture_dep\n")
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": [
                    {
                        "name": "fixture_dep",
                        "type": "git",
                        "rev": "a" * 40,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    calls = []
    def run(command, project_root, *, deadline, **kwargs):
        calls.append(command)
        if command[:4] == ["git", "-C", str(package.resolve()), "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, ("a" * 40 + "\n").encode(), b"")
        if command[:4] == ["git", "-C", str(package.resolve()), "status"]:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, package), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)

    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)

    assert resolved.modules == ("Fixture",)
    assert ["lake", "--rehash", "--no-build", "build", "+Fixture:olean"] in calls
    assert ["git", "-C", str(package.resolve()), "rev-parse", "HEAD"] in calls


def test_git_package_dirtying_during_validation_is_rejected(tmp_path, monkeypatch):
    project = _project(tmp_path)
    package = project / ".lake" / "packages" / "fixture_dep"
    _module(package, "Fixture")
    (package / "lakefile.lean").write_text("package fixture_dep\n")
    revision = "a" * 40
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": [
                    {
                        "name": "fixture_dep",
                        "type": "git",
                        "rev": revision,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status_calls = 0

    def run(command, project_root, *, deadline, **kwargs):
        nonlocal status_calls
        if command[:4] == ["git", "-C", str(package.resolve()), "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, f"{revision}\n".encode(), b"")
        if command[:4] == ["git", "-C", str(package.resolve()), "status"]:
            status_calls += 1
            output = b"" if status_calls == 1 else b" M LakeHelper.lean\n"
            return subprocess.CompletedProcess(command, 0, output, b"")
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, package), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)

    with pytest.raises(LeanImportError, match="configuration is dirty"):
        resolve_project_imports(project, ("Fixture",), timeout=1)

    assert status_calls == 2


def test_cached_imports_reject_a_dirty_executable_git_package(tmp_path, monkeypatch):
    project = _project(tmp_path)
    package = project / ".lake" / "packages" / "fixture_dep"
    _module(package, "Fixture")
    (package / "lakefile.lean").write_text("package fixture_dep\n")
    revision = "a" * 40
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": [
                    {"name": "fixture_dep", "type": "git", "rev": revision}
                ],
            }
        ),
        encoding="utf-8",
    )
    dirty = False
    env_calls = 0

    def run(command, project_root, *, deadline, **kwargs):
        nonlocal env_calls
        if command[:4] == ["git", "-C", str(package.resolve()), "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, f"{revision}\n".encode(), b"")
        if command[:4] == ["git", "-C", str(package.resolve()), "status"]:
            output = b" M LakeHelper.lean\n" if dirty else b""
            return subprocess.CompletedProcess(command, 0, output, b"")
        if command[-1] == "env":
            env_calls += 1
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, package), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolve_project_imports(project, ("Fixture",), timeout=1)
    dirty = True

    with pytest.raises(LeanImportError, match="configuration is dirty"):
        resolve_project_imports(project, ("Fixture",), timeout=1)

    assert env_calls == 1


def test_resolved_imports_recheck_git_generation_after_closure_scan(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    package = project / ".lake" / "packages" / "fixture_dep"
    _module(package, "Fixture")
    (package / "lakefile.lean").write_text("package fixture_dep\n")
    revision = "a" * 40
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packagesDir": ".lake/packages",
                "packages": [
                    {"name": "fixture_dep", "type": "git", "rev": revision}
                ],
            }
        ),
        encoding="utf-8",
    )
    dirty = False

    def run(command, project_root, *, deadline, **kwargs):
        if command[:4] == ["git", "-C", str(package.resolve()), "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, f"{revision}\n".encode(), b"")
        if command[:4] == ["git", "-C", str(package.resolve()), "status"]:
            output = b" M LakeHelper.lean\n" if dirty else b""
            return subprocess.CompletedProcess(command, 0, output, b"")
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, package), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    original_snapshot = repl_imports._snapshot_dependency_closure

    def dirty_after_snapshot(*args, **kwargs):
        nonlocal dirty
        snapshot = original_snapshot(*args, **kwargs)
        dirty = True
        return snapshot

    monkeypatch.setattr(
        repl_imports,
        "_snapshot_dependency_closure",
        dirty_after_snapshot,
    )

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="configuration is dirty",
    ):
        resolved.assert_current(time.monotonic() + 1)


def test_lake_transitive_query_is_exact_and_non_building(tmp_path, monkeypatch):
    project = _project(tmp_path)
    calls = []

    def run(command, project_root, *, deadline, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            b'["Fixture.Dependency","Shared"]\n["Shared"]\n',
            b"",
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)

    assert _REAL_QUERY_TRANSITIVE_MODULES(
        project,
        ("Fixture", "Other"),
        time.monotonic() + 1,
    ) == ("Fixture", "Other", "Fixture.Dependency", "Shared")
    assert calls == [[
        "lake",
        "--no-build",
        "query",
        "+Fixture:transImports",
        "+Other:transImports",
        "--json",
    ]]


def test_toolchain_import_is_bound_without_a_lake_workspace_target(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    root = tmp_path / "toolchain"
    artifact_root = root / "lib" / "lean"
    artifact_root.mkdir(parents=True)
    (artifact_root / "Init.olean").write_bytes(b"builtin")
    (artifact_root / "Init.olean.private").write_bytes(b"private")
    source_root = tmp_path / "sources"
    source_root.mkdir()
    calls = []

    def run(command, project_root, *, deadline, **kwargs):
        calls.append(command)
        environment = (
            f"LEAN_PATH={artifact_root}\n"
            f"LEAN_SRC_PATH={source_root}\n"
            f"LEAN_SYSROOT={root}\n"
        ).encode()
        return subprocess.CompletedProcess(command, 0, environment, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Init",), timeout=1)

    assert calls == [["lake", "--no-build", "env"]]
    assert resolved.modules == ("Init",)
    resolved.assert_current(time.monotonic() + 1)


def test_concurrent_resolution_single_flights_lake_validation(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    build_entered = threading.Event()
    release_build = threading.Event()
    build_calls = 0
    results = []

    def run(command, project_root, *, deadline, **kwargs):
        nonlocal build_calls
        if "build" in command:
            build_calls += 1
            build_entered.set()
            assert release_build.wait(timeout=2)
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)

    def resolve():
        results.append(resolve_project_imports(project, ("Fixture",), timeout=2))

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    threads[0].start()
    assert build_entered.wait(timeout=1)
    threads[1].start()
    release_build.set()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert build_calls == 1


def test_unchanged_resolution_cache_runs_no_lake_subprocess(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    calls = []

    def run(command, project_root, *, deadline, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    first = resolve_project_imports(project, ("Fixture",), timeout=1)
    calls.clear()

    second = resolve_project_imports(project, ("Fixture",), timeout=1)

    assert calls == []
    assert first is not second
    assert first == second


def test_path_dependency_config_is_bound_and_never_skips_lake(tmp_path, monkeypatch):
    project = _project(tmp_path)
    package = tmp_path / "dep"
    _module(package, "Fixture")
    config = package / "lakefile.toml"
    config.write_text('name = "Dep"\n')
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packages": [
                    {
                        "name": "dep",
                        "type": "path",
                        "dir": "../dep",
                        "configFile": "lakefile.toml",
                    }
                ],
            }
        )
    )
    build_calls = 0

    def run(command, project_root, *, deadline, **kwargs):
        nonlocal build_calls
        if "build" in command:
            build_calls += 1
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, package), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    config.write_text('name = "Dep"\nmoreLeanArgs = ["-Dchanged=true"]\n')

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="configuration changed",
    ):
        resolved.assert_current(time.monotonic() + 1)
    resolve_project_imports(project, ("Fixture",), timeout=1)
    assert build_calls == 2


def test_dependency_config_change_during_lake_validation_is_rejected(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    package = tmp_path / "dep"
    _module(package, "Fixture")
    config = package / "lakefile.toml"
    config.write_text('name = "Dep"\n')
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packages": [
                    {
                        "name": "dep",
                        "type": "path",
                        "dir": "../dep",
                        "configFile": "lakefile.toml",
                    }
                ],
            }
        )
    )

    def run(command, project_root, *, deadline, **kwargs):
        if "build" in command:
            config.write_text('name = "Dep"\nmoreLeanArgs = ["-Dchanged=true"]\n')
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, package), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)

    with pytest.raises(LeanImportError, match="configuration changed"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_lake_runner_stops_oversized_output(tmp_path):
    with pytest.raises(LeanImportError, match="exceeded the size limit"):
        repl_imports._run_lake(
            [
                os.sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))",
            ],
            tmp_path,
            timeout=5,
        )


def test_lake_runner_stops_on_timeout(tmp_path):
    with pytest.raises(TimeoutError, match="timed out"):
        repl_imports._run_lake(
            [os.sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            timeout=0.1,
        )


def test_lake_runner_reaps_with_a_separate_post_deadline_budget(
    tmp_path,
    monkeypatch,
):
    wait_timeouts = []
    stdout = (tmp_path / "stdout").open("w+b")
    stderr = (tmp_path / "stderr").open("w+b")

    class Process:
        pid = 12345

        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr

        def poll(self):
            return None

        def wait(self, *, timeout):
            wait_timeouts.append(timeout)
            raise subprocess.TimeoutExpired("fake", timeout)

    class Selector:
        def __init__(self):
            self.mapping = {}

        def register(self, stream, _events):
            self.mapping[stream] = stream

        def get_map(self):
            return self.mapping

        def select(self, _timeout):
            return []

        def close(self):
            pass

    clock = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(repl_imports.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(repl_imports.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(repl_imports.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(repl_imports.os, "killpg", lambda *args: None)

    with pytest.raises(TimeoutError, match="timed out"):
        repl_imports._run_lake(["lake", "env"], tmp_path, timeout=1.0)

    assert wait_timeouts == [repl_imports._PROCESS_KILL_WAIT_SECONDS]


def test_lake_runner_charges_environment_setup_to_its_timeout(tmp_path, monkeypatch):
    clock = {"now": 0.0}
    clean_environment = repl_imports.clean_lake_environment

    def delayed_environment():
        clock["now"] = 2.0
        return clean_environment()

    monkeypatch.setattr(repl_imports.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl_imports, "clean_lake_environment", delayed_environment)

    with pytest.raises(TimeoutError, match="timed out|no request time"):
        repl_imports._run_lake(
            [os.sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            timeout=1.0,
        )


def test_lake_runner_kills_descendants_without_exceeding_deadline(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); "
        "open(sys.argv[1], 'w').write(str(child.pid))"
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        repl_imports._run_lake(
            [os.sys.executable, "-c", script, str(pid_file)],
            tmp_path,
            timeout=1.0,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("Lake descendant survived timed-out process-group cleanup")


def test_resolver_requires_an_existing_manifest_before_running_lake(tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / "lake-manifest.json").unlink()
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("Lake must not run without a manifest"),
    )

    with pytest.raises(LeanImportError, match="lake update.*lake build"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_structured_imports_require_a_project_toolchain_pin(tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / "lean-toolchain").unlink()
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("Lake must not run without a toolchain pin"),
    )

    with pytest.raises(LeanImportError, match="lean-toolchain pin"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


@pytest.mark.parametrize(
    "name",
    ["lean-toolchain", "lake-manifest.json", "lakefile.toml"],
)
def test_project_control_files_reject_fifos_without_blocking(
    tmp_path,
    monkeypatch,
    name,
):
    project = _project(tmp_path)
    control = project / name
    control.unlink()
    os.mkfifo(control)
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("a FIFO must be rejected before Lake runs"),
    )

    with pytest.raises(LeanImportError, match="not a regular file"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("lean-toolchain", b"leanprover/lean4:v4.31.0\n"),
        ("lake-manifest.json", b'{"version":"1.1.0","packages":[]}\n'),
        ("lakefile.toml", b'name = "Replacement"\n'),
    ],
)
def test_project_control_files_reject_concurrent_regular_replacement(
    tmp_path,
    monkeypatch,
    name,
    replacement,
):
    project = _project(tmp_path).resolve()
    control = project / name
    staged = project / f"replacement-{name}"
    staged.write_bytes(replacement)
    real_open = os.open
    replaced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if not replaced and dir_fd is not None and os.fspath(path) == name:
            replaced = True
            os.replace(staged, control)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(repl_imports.os, "open", racing_open)

    with pytest.raises(LeanImportError, match="changed while it was inspected"):
        if name == "lean-toolchain":
            repl_imports._require_toolchain_pin(control, time.monotonic() + 1)
        elif name == "lake-manifest.json":
            repl_imports._read_package_specs(
                project,
                control,
                time.monotonic() + 1,
            )
        else:
            repl_imports._require_project_configuration_files(
                project,
                time.monotonic() + 1,
            )

    assert replaced


@pytest.mark.parametrize("name", ["lean-toolchain", "lake-manifest.json"])
def test_bounded_project_file_reads_reject_concurrent_fifo_replacement(
    tmp_path,
    monkeypatch,
    name,
):
    project = _project(tmp_path).resolve()
    control = project / name
    fifo = project / f"replacement-{name}"
    os.mkfifo(fifo)
    real_open = os.open
    replaced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if not replaced and dir_fd is not None and os.fspath(path) == name:
            replaced = True
            assert flags & os.O_NONBLOCK
            os.replace(fifo, control)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(repl_imports.os, "open", racing_open)

    with pytest.raises(LeanImportError, match="changed|regular file"):
        if name == "lean-toolchain":
            repl_imports._require_toolchain_pin(control, time.monotonic() + 1)
        else:
            repl_imports._read_package_specs(
                project,
                control,
                time.monotonic() + 1,
            )

    assert replaced


@pytest.mark.parametrize(
    ("name", "limit"),
    [
        ("lean-toolchain", 4096),
        ("lake-manifest.json", repl_imports._MAX_MANIFEST_BYTES),
    ],
)
def test_bounded_project_file_reads_do_not_read_concurrent_oversize_replacement(
    tmp_path,
    monkeypatch,
    name,
    limit,
):
    project = _project(tmp_path).resolve()
    control = project / name
    staged = project / f"replacement-{name}"
    staged.write_bytes(b"x" * (limit + 1))
    real_open = os.open
    replaced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if not replaced and dir_fd is not None and os.fspath(path) == name:
            replaced = True
            os.replace(staged, control)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(repl_imports.os, "open", racing_open)
    monkeypatch.setattr(
        repl_imports.os,
        "read",
        lambda *args: pytest.fail("a replaced oversized file must not be read"),
    )

    with pytest.raises(LeanImportError, match="changed|size limit"):
        if name == "lean-toolchain":
            repl_imports._require_toolchain_pin(control, time.monotonic() + 1)
        else:
            repl_imports._read_package_specs(
                project,
                control,
                time.monotonic() + 1,
            )

    assert replaced


@pytest.mark.parametrize(
    ("name", "limit"),
    [
        ("lean-toolchain", 4096),
        ("lake-manifest.json", repl_imports._MAX_MANIFEST_BYTES),
    ],
)
def test_bounded_project_file_reads_reject_oversized_regular_files(
    tmp_path,
    monkeypatch,
    name,
    limit,
):
    project = _project(tmp_path)
    (project / name).write_bytes(b"x" * (limit + 1))
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("oversized controls must fail before Lake"),
    )

    with pytest.raises(LeanImportError, match="size limit"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_bounded_project_file_reads_charge_the_request_deadline(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path).resolve()
    real_read = os.read
    clock = {"now": 0.0}

    def delayed_read(descriptor, length):
        data = real_read(descriptor, length)
        clock["now"] = 2.0
        return data

    monkeypatch.setattr(repl_imports.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl_imports.os, "read", delayed_read)

    with pytest.raises(TimeoutError, match="timed out"):
        repl_imports._require_toolchain_pin(project / "lean-toolchain", 1.0)


@pytest.mark.parametrize(
    "failure",
    ["missing", "stale", "ambiguous", "escape", "loop"],
)
def test_resolver_rejects_untrusted_or_unusable_artifacts(tmp_path, monkeypatch, failure):
    project = _project(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    roots = [first]
    (first / "artifacts").mkdir(parents=True)
    (first / "sources").mkdir(parents=True)
    if failure != "missing":
        _module(first, "Fixture", source_time=3 if failure == "stale" else 1)
    if failure == "ambiguous":
        _module(second, "Fixture")
        roots.append(second)
    if failure == "escape":
        artifact = first / "artifacts" / "Fixture.olean"
        artifact.unlink()
        outside = tmp_path / "outside.olean"
        outside.write_bytes(b"outside")
        artifact.symlink_to(outside)
    if failure == "loop":
        artifact = first / "artifacts" / "Fixture.olean"
        artifact.unlink()
        artifact.symlink_to(artifact)
    def run(command, project_root, *, deadline, **kwargs):
        if command[-1] == "env":
            return subprocess.CompletedProcess(command, 0, _lake_environment(*roots), b"")
        returncode = 3 if failure == "stale" else 0
        return subprocess.CompletedProcess(command, returncode, b"", b"out of date")

    monkeypatch.setattr(repl_imports, "_run_lake", run)

    expected = {
        "missing": "not built",
        "stale": "stale",
        "ambiguous": "ambiguous",
        "escape": "escapes",
        "loop": "escapes",
    }[failure]
    with pytest.raises(LeanImportError, match=expected):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_manifest_mutation_during_lake_discovery(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline, **kwargs):
        (project / "lake-manifest.json").write_text("changed\n")
        return subprocess.CompletedProcess(command, 0, _lake_environment(root), b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="changed during import discovery"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_oversized_manifest_before_running_lake(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    with (project / "lake-manifest.json").open("wb") as stream:
        stream.truncate(repl_imports._MAX_MANIFEST_BYTES + 1)
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("Lake must not read an oversized manifest"),
    )

    with pytest.raises(LeanImportError, match="manifest.*size limit"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_non_object_manifest(tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / "lake-manifest.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("malformed manifest must not reach Lake"),
    )

    with pytest.raises(LeanImportError, match="malformed package metadata"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


@pytest.mark.parametrize("mutation_stage", ["env", "build"])
def test_resolver_rejects_config_replacement_during_discovery(
    tmp_path, monkeypatch, mutation_stage
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    config = project / "lakefile.toml"
    original_mtime = config.stat().st_mtime_ns

    def run(command, project_root, *, deadline, **kwargs):
        stage = "env" if command[-1] == "env" else "build"
        if stage == mutation_stage:
            replacement = project / "lakefile.replacement"
            replacement.write_text('name = "Changed"\n', encoding="utf-8")
            os.utime(replacement, ns=(original_mtime, original_mtime))
            replacement.replace(config)
        stdout = _fake_lake_stdout(command, root)
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="project changed during import discovery"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_root_replacement_during_discovery(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline, **kwargs):
        moved = tmp_path / "original-project"
        project.rename(moved)
        replacement = _project(tmp_path)
        assert replacement == project
        return subprocess.CompletedProcess(command, 0, _lake_environment(root), b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="project changed during import discovery"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_artifact_replacement_after_lake_check(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    artifact = root / "artifacts" / "Fixture.olean"
    outside = tmp_path / "outside.olean"
    outside.write_bytes(b"outside")

    def run(command, project_root, *, deadline, **kwargs):
        if command[-1] == "env":
            return subprocess.CompletedProcess(command, 0, _lake_environment(root), b"")
        if command[-1] != "env":
            artifact.unlink()
            artifact.symlink_to(outside)
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="not a regular file|changed"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolved_imports_reject_artifact_replacement(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline, **kwargs):
        stdout = _fake_lake_stdout(command, root)
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    artifact = root / "artifacts" / "Fixture.olean"
    artifact.write_bytes(b"replaced")

    with pytest.raises(repl_imports.StaleResolvedImportsError, match="stale"):
        resolved.assert_current(time.monotonic() + 1)


@pytest.mark.parametrize(
    ("operation", "trigger_after"),
    [
        ("direct-artifact", 1),
        ("transitive-source", 1),
        ("transitive-artifact", 1),
        ("transitive-companion", 2),
    ],
)
def test_descriptor_bound_import_files_reject_intermediate_directory_replacement(
    tmp_path,
    monkeypatch,
    operation,
    trigger_after,
):
    artifact_root = (tmp_path / "root" / "artifacts").resolve()
    source_root = (tmp_path / "root" / "sources").resolve()
    artifact_directory = artifact_root / "Nested"
    source_directory = source_root / "Nested"
    artifact_directory.mkdir(parents=True)
    source_directory.mkdir(parents=True)
    main = artifact_directory / "Dependency.olean"
    main.write_bytes(b"olean")
    main.with_suffix(".trace").write_text("{}", encoding="utf-8")
    Path(f"{main}.server").write_bytes(b"server")
    source = source_directory / "Dependency.lean"
    source.write_text("theorem dependency : True := by trivial\n", encoding="utf-8")

    active_root = source_root if operation == "transitive-source" else artifact_root
    original = active_root / "Nested"
    staged = active_root / "Nested-replacement"
    saved = active_root / "Nested-original"
    shutil.copytree(original, staged)
    expected_parent = active_root.stat()
    real_open = os.open
    opens = 0
    replaced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal opens, replaced
        if (
            not replaced
            and dir_fd is not None
            and os.fspath(path) == "Nested"
            and (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino)
            == (expected_parent.st_dev, expected_parent.st_ino)
        ):
            opens += 1
            if opens == trigger_after:
                replaced = True
                original.rename(saved)
                staged.rename(original)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(repl_imports.os, "open", racing_open)

    with pytest.raises(LeanImportError, match="changed|symbolic link"):
        deadline = time.monotonic() + 1
        if operation == "direct-artifact":
            repl_imports._direct_artifact_identities(main, artifact_root, deadline)
        elif operation == "transitive-source":
            repl_imports._require_unique_bound_file(
                (source_root,),
                Path("Nested/Dependency.lean"),
                0,
                deadline,
            )
        elif operation == "transitive-artifact":
            repl_imports._require_unique_bound_file(
                (artifact_root,),
                Path("Nested/Dependency.olean"),
                0,
                deadline,
            )
        else:
            repl_imports._known_artifact_identities(
                main,
                artifact_root,
                deadline,
            )

    assert replaced


def test_resolved_imports_bind_transitive_olean_dependencies(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture", imports=("Fixture.Dependency",))
    _module(root, "Fixture.Dependency")
    dependency = root / "artifacts" / "Fixture" / "Dependency.olean"

    def run(command, project_root, *, deadline, **kwargs):
        if command[-1] == "env":
            stdout = _lake_environment(root)
        else:
            stdout = b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    direct_identity = resolved._selections[0].artifact

    dependency.write_bytes(b"rebuilt dependency")

    assert resolved._selections[0].artifact == direct_identity
    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="dependency files changed",
    ):
        resolved.assert_current(time.monotonic() + 1)


@pytest.mark.parametrize(
    "relative",
    [
        "Fixture/Dependency.lean",
        "Fixture/Dependency.olean.server",
        "Fixture/Dependency.olean.private",
        "Fixture/Dependency.ilean",
        "Fixture/Dependency.ir",
    ],
)
def test_resolved_imports_reject_transitive_source_and_companion_changes(
    tmp_path, monkeypatch, relative
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture", imports=("Fixture.Dependency",))
    _module(root, "Fixture.Dependency")
    artifact = root / "artifacts" / "Fixture" / "Dependency.olean"
    Path(f"{artifact}.server").write_bytes(b"server")
    Path(f"{artifact}.private").write_bytes(b"private")
    artifact.with_suffix(".ilean").write_bytes(b"ilean")
    Path(f"{artifact.with_suffix('.ilean')}.hash").write_bytes(b"ilean hash")
    artifact.with_suffix(".ir").write_bytes(b"ir")

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    changed = root / ("sources" if relative.endswith(".lean") else "artifacts") / relative
    original_mtime = changed.stat().st_mtime_ns
    changed.write_bytes(changed.read_bytes() + b" changed")
    os.utime(changed, ns=(original_mtime, original_mtime))

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="dependency files changed",
    ):
        resolved.assert_current(time.monotonic() + 1)


def test_resolved_imports_ignore_unrelated_module_changes(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture", imports=("Fixture.Dependency",))
    _module(root, "Fixture.Dependency")
    _module(root, "Unrelated")

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    unrelated_source = root / "sources" / "Unrelated.lean"
    unrelated_artifact = root / "artifacts" / "Unrelated.olean"
    unrelated_source.write_text("theorem unrelated : False := by sorry\n")
    unrelated_artifact.write_bytes(b"rebuilt unrelated module")

    resolved.assert_current(time.monotonic() + 1)


def test_resolver_ignores_unrelated_source_change_during_lake_check(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    _module(root, "Unrelated")
    unrelated = root / "sources" / "Unrelated.lean"

    def run(command, project_root, *, deadline, **kwargs):
        if command[-1] != "env":
            unrelated.write_text("theorem changed : True := by trivial\n")
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)

    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    resolved.assert_current(time.monotonic() + 1)


def test_resolver_rejects_transitive_source_change_during_lake_check(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture", imports=("Fixture.Dependency",))
    _module(root, "Fixture.Dependency")
    dependency = root / "sources" / "Fixture" / "Dependency.lean"

    def run(command, project_root, *, deadline, **kwargs):
        if command[-1] != "env":
            dependency.write_text("theorem changed : True := by trivial\n")
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="dependency sources changed"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rechecks_closure_after_a_concurrent_rebuild(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture", imports=("Dependency",))
    _module(root, "Dependency")
    _module(root, "New")
    dependency_source = root / "sources" / "Dependency.lean"
    dependency_artifact = root / "artifacts" / "Dependency.olean"
    original_query = repl_imports._query_transitive_modules
    query_calls = 0

    def query(project_root, modules, deadline):
        nonlocal query_calls
        query_calls += 1
        closure = original_query(project_root, modules, deadline)
        if query_calls == 1:
            dependency_source.write_text(
                "import New\ntheorem fixture : True := by trivial\n",
                encoding="utf-8",
            )
            dependency_artifact.write_bytes(b"rebuilt with New")
            dependency_artifact.with_suffix(".trace").write_text(
                '{"schemaVersion":"test","depHash":"rebuilt","outputs":{}}',
                encoding="utf-8",
            )
        return closure

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_query_transitive_modules", query)
    monkeypatch.setattr(repl_imports, "_run_lake", run)

    with pytest.raises(LeanImportError, match="import closure changed"):
        resolve_project_imports(project, ("Fixture",), timeout=1)

    assert query_calls == 2


def test_resolved_imports_bind_direct_companions_and_lake_trace(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    main = root / "artifacts" / "Fixture.olean"
    Path(f"{main}.server").write_bytes(b"server")
    Path(f"{main}.private").write_bytes(b"private")
    main.with_suffix(".ir").write_bytes(b"ir")

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)

    assert {artifact.path.name for artifact in resolved._dependencies[0].artifacts} == {
        "Fixture.olean",
        "Fixture.olean.server",
        "Fixture.olean.private",
        "Fixture.ir",
        "Fixture.trace",
    }


def test_resolver_requires_the_direct_lake_build_trace(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    (root / "artifacts" / "Fixture.trace").unlink()

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="build trace is missing"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolved_imports_reject_a_new_direct_olean_companion(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    Path(f"{root / 'artifacts' / 'Fixture.olean'}.server").write_bytes(b"server")

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="dependency files changed",
    ):
        resolved.assert_current(time.monotonic() + 1)


def test_resolved_imports_reject_retargeted_lake_root_alias(tmp_path, monkeypatch):
    project = _project(tmp_path)
    first = tmp_path / "first-root"
    second = tmp_path / "second-root"
    _module(first, "Fixture")
    _module(second, "Fixture")
    alias = tmp_path / "lake-root"
    alias.symlink_to(first, target_is_directory=True)

    def run(command, project_root, *, deadline, **kwargs):
        stdout = _fake_lake_stdout(command, alias)
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="Lake import root changed",
    ):
        resolved.assert_current(time.monotonic() + 1)


def test_resolved_imports_recheck_lake_roots_after_closure_scan(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    first = tmp_path / "first-root"
    second = tmp_path / "second-root"
    _module(first, "Fixture")
    _module(second, "Fixture")
    alias = tmp_path / "lake-root"
    alias.symlink_to(first, target_is_directory=True)

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, alias), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    original_snapshot = repl_imports._snapshot_dependency_closure

    def retarget_after_snapshot(*args, **kwargs):
        snapshot = original_snapshot(*args, **kwargs)
        alias.unlink()
        alias.symlink_to(second, target_is_directory=True)
        return snapshot

    monkeypatch.setattr(
        repl_imports,
        "_snapshot_dependency_closure",
        retarget_after_snapshot,
    )

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="Lake import root changed",
    ):
        resolved.assert_current(time.monotonic() + 1)


def test_resolved_imports_reject_new_earlier_lake_root(tmp_path, monkeypatch):
    project = _project(tmp_path)
    missing = tmp_path / "missing-root"
    existing = tmp_path / "existing-root"
    _module(existing, "Fixture")

    def run(command, project_root, *, deadline, **kwargs):
        stdout = _fake_lake_stdout(command, missing, existing)
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    _module(missing, "Fixture")

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="Lake import root changed",
    ):
        resolved.assert_current(time.monotonic() + 1)


def test_resolver_checks_deadline_after_final_lake_call(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    clock = {"now": 0.0}

    def run(command, project_root, *, deadline, **kwargs):
        if command[-1] == "env":
            return subprocess.CompletedProcess(command, 0, _lake_environment(root), b"")
        clock["now"] = 2.0
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(repl_imports.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(TimeoutError, match="timed out"):
        resolve_project_imports(project, ("Fixture",), deadline=1.0)


def test_dependency_tree_scan_preserves_deadline_errors(tmp_path, monkeypatch):
    root = tmp_path / "root"
    child = root / "Fixture"
    child.mkdir(parents=True)
    (child / "Dependency.olean").write_bytes(b"artifact")

    def time_out(*args, **kwargs):
        raise TimeoutError("deadline exhausted")

    monkeypatch.setattr(repl_imports, "_inspect_regular_at", time_out)

    with pytest.raises(TimeoutError, match="deadline exhausted"):
        repl_imports._snapshot_dependency_roots(
            (root.resolve(),),
            time.monotonic() + 1,
        )


def test_resolved_imports_reject_changed_project_config(tmp_path):
    project = _project(tmp_path).resolve()
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline, **kwargs):
        stdout = _fake_lake_stdout(command, root)
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repl_imports, "_run_lake", run)
        resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    (project / "lakefile.toml").write_text('name = "Changed"\n', encoding="utf-8")

    with pytest.raises(repl_imports.StaleResolvedImportsError, match="stale"):
        resolved.assert_current(time.monotonic() + 1)


@pytest.mark.parametrize(
    "name",
    ["lakefile.toml", "lake-manifest.json", "lean-toolchain"],
)
def test_resolved_imports_reject_project_config_symlink_replacement(
    tmp_path,
    monkeypatch,
    name,
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    config = project / name
    saved = project / f"saved-{name}"
    config.rename(saved)
    config.symlink_to(saved.name)

    with pytest.raises(repl_imports.StaleResolvedImportsError, match="stale"):
        resolved.assert_current(time.monotonic() + 1)


def test_resolved_imports_recheck_project_config_after_closure_scan(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, _fake_lake_stdout(command, root), b""
        )

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    original_snapshot = repl_imports._snapshot_dependency_closure
    config = project / "lakefile.toml"

    def mutate_after_snapshot(*args, **kwargs):
        snapshot = original_snapshot(*args, **kwargs)
        config.write_text('name = "Changed"\n', encoding="utf-8")
        return snapshot

    monkeypatch.setattr(
        repl_imports,
        "_snapshot_dependency_closure",
        mutate_after_snapshot,
    )

    with pytest.raises(repl_imports.StaleResolvedImportsError, match="stale"):
        resolved.assert_current(time.monotonic() + 1)


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_resolver_accepts_a_fresh_local_module_and_rejects_stale_sources(
    tmp_path,
):
    project = tmp_path / "fixture"
    project.mkdir()
    (project / "lean-toolchain").write_text(
        "leanprover/lean4:v4.32.2\n",
        encoding="utf-8",
    )
    (project / "lakefile.toml").write_text(
        """name = "Fixture"
version = "0.1.0"
defaultTargets = ["Fixture"]

[[lean_lib]]
name = "Fixture"
srcDir = "src"
""",
        encoding="utf-8",
    )
    source = project / "src" / "Fixture.lean"
    source.parent.mkdir()
    dependency = project / "src" / "Fixture" / "Dependency.lean"
    dependency.parent.mkdir()
    dependency.write_text(
        "namespace Fixture\n\ndef dependencyValue : Nat := 5\n\nend Fixture\n",
        encoding="utf-8",
    )
    source.write_text(
        "import Fixture.Dependency\n\n"
        "namespace Fixture\n\ndef localValue : Nat := dependencyValue + 32\n\n"
        "end Fixture\n",
        encoding="utf-8",
    )

    for command in (["lake", "update"], ["lake", "build", "Fixture"]):
        result = subprocess.run(
            command,
            cwd=project,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    resolved = resolve_project_imports(project, ("Fixture",), timeout=60)
    assert resolved.project_root == project.resolve()
    assert resolved.modules == ("Fixture",)
    resolved.assert_current(time.monotonic() + 60)

    dependency_timestamp = dependency.stat().st_mtime_ns
    dependency.write_text(
        "namespace Fixture\n\ndef dependencyValue : Nat := 6\n\nend Fixture\n",
        encoding="utf-8",
    )
    os.utime(dependency, ns=(dependency_timestamp, dependency_timestamp))
    with pytest.raises(LeanImportError, match="dependencies are stale"):
        resolve_project_imports(project, ("Fixture",), timeout=30)

    rebuilt = subprocess.run(
        ["lake", "build", "Fixture"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr

    refreshed = resolve_project_imports(project, ("Fixture",), timeout=60)
    assert refreshed != resolved
    refreshed.assert_current(time.monotonic() + 60)
