from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph
from autoform_cli.project import ProjectCreateError, create_project, inspect_project
from autoform_cli.project import create as create_module

_RELEASE = "lean-v4.32.2-mathlib-v4.32.2"


def test_creation_never_discovers_git_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

    def forbidden():
        raise AssertionError("project new invoked Git-backed plugin discovery")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    create_project(tmp_path / "Project", package="Project", release_id=_RELEASE)


def test_creates_complete_supported_project(tmp_path: Path) -> None:
    target = tmp_path / "FiniteFlat"
    result = create_project(target, package="FiniteFlat", release_id=_RELEASE)

    assert result.package == "FiniteFlat"
    assert result.release == _RELEASE
    assert result.target == "FiniteFlat"
    assert (target / "lean-toolchain").read_text(encoding="utf-8") == (
        "leanprover/lean4:v4.32.2\n"
    )
    assert (target / "lakefile.toml").read_text(encoding="utf-8") == (
        'name = "FiniteFlat"\n'
        'version = "0.1.0"\n'
        'defaultTargets = ["FiniteFlat"]\n\n'
        '[[require]]\n'
        'name = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        'rev = "v4.32.2"\n\n'
        '[[lean_lib]]\n'
        'name = "FiniteFlat"\n'
        'srcDir = "src"\n'
    )
    assert (target / "src/FiniteFlat.lean").read_text(encoding="utf-8") == (
        "import Mathlib\n\n"
        "namespace FiniteFlat\n\n"
        "/-- Marker declaration for the initial project build. -/\n"
        "def autoformProjectInitialized : Bool := true\n\n"
        "end FiniteFlat\n"
    )
    inspection = inspect_project(target)
    assert inspection.ok
    assert inspection.compatibility.status == "supported"
    assert inspection.compatibility.release == _RELEASE
    assert set(load_graph(target / "blueprint").nodes) == {"roadmap"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not list(tmp_path.glob(".FiniteFlat.autoform-new-*"))


@pytest.mark.parametrize(
    "package",
    [
        "",
        "finiteFlat",
        "Finite_Flat",
        "Finite.Flat",
        "../FiniteFlat",
        "Finite Flat",
        'Finite"Flat',
        "Type",
        "Sort",
        "Prop",
    ],
)
def test_rejects_invalid_package_before_writing(tmp_path: Path, package: str) -> None:
    target = tmp_path / "project"
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package=package, release_id=_RELEASE)
    assert raised.value.code == "project-name-invalid"
    assert not target.exists()
    assert not list(tmp_path.glob(".project.autoform-new-*"))


def test_rejects_unknown_release_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "project"
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id="unknown")
    assert raised.value.code == "project-release-unknown"
    assert not target.exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken-symlink"])
def test_never_overwrites_existing_target(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "project"
    if kind == "file":
        target.write_bytes(b"authored\n")
    elif kind == "directory":
        target.mkdir()
        (target / "authored").write_bytes(b"authored\n")
    else:
        real = tmp_path / "real"
        if kind == "symlink":
            real.mkdir()
        target.symlink_to(real, target_is_directory=True)
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes())
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    )

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-target-exists"
    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes())
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    assert after == before


def test_normal_macos_tmp_alias_is_supported() -> None:
    if not Path("/tmp").is_symlink():
        pytest.skip("platform has no /tmp alias")
    parent = Path("/tmp") / f"autoform-new-test-{os.getpid()}"
    parent.mkdir()
    target = parent / "Project"
    try:
        create_project(target, package="Project", release_id=_RELEASE)
        assert inspect_project(parent.resolve() / "Project").ok
    finally:
        import shutil

        shutil.rmtree(parent, ignore_errors=True)


def test_rejects_nonsticky_shared_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(parent / "Project", package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-parent-unsafe"


def test_injected_build_failure_leaves_no_target_or_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(create_module, "_build_staged_project", fail)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    assert not list(tmp_path.glob(".project.autoform-new-*"))


def test_injected_validation_failure_leaves_no_target_or_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"

    def fail(*args, **kwargs):
        raise ProjectCreateError("project-create-validation-failed", "invalid")

    monkeypatch.setattr(create_module, "_validate_staged_project", fail)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-create-validation-failed"
    assert not target.exists()
    assert not list(tmp_path.glob(".project.autoform-new-*"))


def test_workspace_substitution_fails_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._validate_staged_project

    def substitute(stage: Path, release) -> None:
        original(stage, release)
        workspace = stage.parent
        moved = workspace.with_name(f"{workspace.name}-owned")
        workspace.rename(moved)
        workspace.mkdir(mode=0o700)
        (workspace / "FOREIGN").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(create_module, "_validate_staged_project", substitute)
    with pytest.raises(ProjectCreateError):
        create_project(target, package="Project", release_id=_RELEASE)
    assert not target.exists()
    assert any(path.name == "FOREIGN" for path in tmp_path.rglob("FOREIGN"))


def test_cleanup_never_deletes_a_substituted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._remove_owned_stage
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "KEEP").write_text("keep\n", encoding="utf-8")

    def substitute(parent_descriptor, stage_name, stage_descriptor, identity):
        original_name = f"{stage_name}-owned"
        os.rename(stage_name, original_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
        os.mkdir(stage_name, dir_fd=parent_descriptor)
        replacement = tmp_path / stage_name
        (replacement / "FOREIGN").write_text("foreign\n", encoding="utf-8")
        return original(parent_descriptor, stage_name, stage_descriptor, identity)

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(create_module, "_build_staged_project", fail)
    monkeypatch.setattr(create_module, "_remove_owned_stage", substitute)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-cleanup-failed"
    assert (foreign / "KEEP").read_text(encoding="utf-8") == "keep\n"
    assert any(path.name == "FOREIGN" for path in tmp_path.rglob("FOREIGN"))


def test_concurrent_creation_has_exactly_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    barrier = threading.Barrier(2)
    original = create_module._validate_staged_project

    def synchronized(stage: Path, release) -> None:
        original(stage, release)
        barrier.wait(timeout=10)

    monkeypatch.setattr(create_module, "_validate_staged_project", synchronized)
    results: list[str] = []

    def run() -> None:
        try:
            create_project(target, package="Project", release_id=_RELEASE)
            results.append("created")
        except ProjectCreateError as error:
            results.append(error.code)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["created", "project-target-exists"]
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".project.autoform-new-*"))


@pytest.mark.parametrize(
    "arguments, code",
    [
        (["project", "new", "--json"], "project-target-invalid"),
        (["project", "new", "project", "--release", _RELEASE, "--json"], "project-name-invalid"),
        (["project", "new", "project", "--package", "Project", "--json"], "project-release-unknown"),
    ],
)
def test_cli_missing_creation_options_are_json(
    arguments: list[str], code: str, capsys
) -> None:
    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == code
    assert captured.err == ""


def test_cli_json_is_stable_and_path_free(tmp_path: Path, capsys) -> None:
    target = tmp_path / "project"
    assert main(
        [
            "project",
            "new",
            str(target),
            "--package",
            "Project",
            "--release",
            _RELEASE,
            "--json",
        ]
    ) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["target"] == "project"
    assert str(tmp_path) not in captured.out
    assert captured.err == ""

    duplicate = tmp_path / "project"
    assert main(
        [
            "project",
            "new",
            str(duplicate),
            "--package",
            "Project",
            "--release",
            _RELEASE,
            "--json",
        ]
    ) == 1
    failed = capsys.readouterr()
    assert json.loads(failed.out)["error"]["code"] == "project-target-exists"
    assert failed.err == ""
