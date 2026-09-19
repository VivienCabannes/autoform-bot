from __future__ import annotations

import errno
import json
import os
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph
from autoform_cli.project import (
    ProjectCreateError,
    create_project,
    inspect_project,
    load_release_catalog,
)
from autoform_cli.project import create as create_module

_RELEASE = "lean-v4.32.2-mathlib-v4.32.2"


def test_creation_never_discovers_git_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autoform_cli import scaffold as scaffold_module

    def forbidden():
        raise AssertionError("project new invoked Git-backed plugin discovery")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    target = tmp_path / "Project"
    result = create_project(target, package="Project", release_id=_RELEASE)

    assert not result.workflows_pinned
    assert not (target / ".github/workflows/autoform-verify.yml").exists()
    assert not (target / ".github/workflows/blueprint-pages.yml").exists()


def test_creation_accepts_only_a_complete_workflow_pin(tmp_path: Path) -> None:
    target = tmp_path / "Project"
    source = "https://example.test/owner/autoform.git"
    revision = "A" * 40

    result = create_project(
        target,
        package="Project",
        release_id=_RELEASE,
        autoform_source=source,
        autoform_ref=revision,
    )

    assert result.workflows_pinned
    workflow = (target / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f'AUTOFORM_SOURCE: "{source}"' in workflow
    assert f'AUTOFORM_REF: "{revision.lower()}"' in workflow


@pytest.mark.parametrize(
    ("source", "revision"),
    [
        ("https://example.test/owner/autoform.git", ""),
        ("", "1" * 40),
        ("https://example.test/owner/autoform.git", "main"),
        ("https://user:secret@example.test/autoform.git", "1" * 40),
    ],
)
def test_creation_rejects_invalid_provenance_before_writing(source: str, revision: str, tmp_path: Path) -> None:
    target = tmp_path / "Project"

    with pytest.raises(ProjectCreateError) as raised:
        create_project(
            target,
            package="Project",
            release_id=_RELEASE,
            autoform_source=source,
            autoform_ref=revision,
        )

    assert raised.value.code == "project-provenance-invalid"
    assert not target.exists()
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_creation_with_an_explicit_pin_stays_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autoform_cli import provenance, scaffold as scaffold_module

    def forbidden(*args, **kwargs):
        raise AssertionError("project new crossed its offline boundary")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    monkeypatch.setattr(provenance, "verify_plugin_provenance", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    result = create_project(
        tmp_path / "Project",
        package="Project",
        release_id=_RELEASE,
        autoform_source="https://example.test/owner/autoform.git",
        autoform_ref="1" * 40,
    )

    assert result.workflows_pinned


def test_creates_complete_supported_project(tmp_path: Path) -> None:
    target = tmp_path / "FiniteFlat"
    result = create_project(target, package="FiniteFlat", release_id=_RELEASE)

    assert result.package == "FiniteFlat"
    assert result.release == _RELEASE
    assert result.target == "FiniteFlat"
    assert (target / "lean-toolchain").read_text(encoding="utf-8") == ("leanprover/lean4:v4.32.2\n")
    assert (target / "lakefile.toml").read_text(encoding="utf-8") == (
        'name = "FiniteFlat"\n'
        'version = "0.1.0"\n'
        'defaultTargets = ["FiniteFlat"]\n\n'
        "[[require]]\n"
        'name = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        'rev = "v4.32.2"\n\n'
        "[[lean_lib]]\n"
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
    assert not list(tmp_path.glob(".autoform-new-*"))


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
        "Aesop",
        "Batteries",
        "Cache",
        "Cli",
        "ImportGraph",
        "Lean",
        "LeanSearchClient",
        "Lake",
        "Plausible",
        "ProofWidgets",
        "Qq",
        "Std",
        "Type",
        "Sort",
        "Prop",
        "Init",
        "Mathlib",
    ],
)
def test_rejects_invalid_package_before_writing(tmp_path: Path, package: str) -> None:
    target = tmp_path / "project"
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package=package, release_id=_RELEASE)
    assert raised.value.code == "project-name-invalid"
    assert not target.exists()
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_every_release_has_a_module_collision_contract() -> None:
    assert set(create_module._RELEASE_MODULE_ROOTS) == {release.id for release in load_release_catalog().releases}


def test_rejects_non_string_package_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "project"

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package=123, release_id=_RELEASE)  # type: ignore[arg-type]

    assert raised.value.code == "project-name-invalid"
    assert not target.exists()
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_rejects_unknown_release_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "project"
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id="unknown")
    assert raised.value.code == "project-release-unknown"
    assert not target.exists()


def test_long_valid_target_name_does_not_expand_the_stage_name(tmp_path: Path) -> None:
    name_limit = os.pathconf(tmp_path, "PC_NAME_MAX")
    if name_limit < 64:
        pytest.skip("filesystem name limit is too small for this boundary test")
    target = tmp_path / ("p" * name_limit)

    result = create_project(target, package="Project", release_id=_RELEASE)

    assert result.target == target.name
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_embedded_nul_target_uses_the_stable_error_contract(tmp_path: Path, capsys) -> None:
    target = os.fspath(tmp_path / "bad\0name")

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-target-invalid"

    assert (
        main(
            [
                "project",
                "new",
                target,
                "--package",
                "Project",
                "--release",
                _RELEASE,
                "--json",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "project-target-invalid"


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


def test_missing_directory_capability_uses_stable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    monkeypatch.setattr(
        create_module.os,
        "supports_dir_fd",
        create_module.os.supports_dir_fd - {create_module.os.mkdir},
    )

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-safety-unavailable"
    assert not target.exists()
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_injected_build_failure_preserves_the_empty_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(create_module, "_materialize_project", fail)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-create-failed"
    assert ".autoform-new-* stage may remain" in raised.value.message
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert not list(stages[0].iterdir())


def test_injected_validation_failure_preserves_stage_for_safe_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"

    def fail(*args, **kwargs):
        raise ProjectCreateError("project-create-validation-failed", "invalid")

    monkeypatch.setattr(create_module, "_validate_staged_project", fail)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-create-validation-failed"
    assert ".autoform-new-* stage may remain" in raised.value.message
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert (stages[0] / "lean-toolchain").is_file()


def test_invalid_planned_roadmap_is_never_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._build_project_plan

    def corrupt(*args, **kwargs):
        plan, pinned = original(*args, **kwargs)
        changed = tuple(
            type(item)(item.relative, b"No H1 title.\n", item.mode)
            if item.relative == "blueprint/roadmap/README.md"
            else item
            for item in plan
        )
        return changed, pinned

    monkeypatch.setattr(create_module, "_build_project_plan", corrupt)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-validation-failed"
    assert not target.exists()
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_close_failure_after_publish_reports_that_the_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original_rename = create_module._rename_noreplace
    original_close = create_module.os.close
    published = False
    failed = False

    def publish(*args):
        nonlocal published
        original_rename(*args)
        published = True

    def close_after_publish(descriptor):
        nonlocal failed
        original_close(descriptor)
        if published and not failed:
            failed = True
            raise OSError("injected close failure")

    monkeypatch.setattr(create_module, "_rename_noreplace", publish)
    monkeypatch.setattr(create_module.os, "close", close_after_publish)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-commit-uncertain"
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_fsync_failure_after_publish_reports_that_the_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original_rename = create_module._rename_noreplace
    original_fsync = create_module.os.fsync
    published = False
    failed = False

    def publish(*args):
        nonlocal published
        original_rename(*args)
        published = True

    def fail_after_publish(descriptor):
        nonlocal failed
        if published and not failed:
            failed = True
            raise OSError("injected fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(create_module, "_rename_noreplace", publish)
    monkeypatch.setattr(create_module.os, "fsync", fail_after_publish)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-commit-uncertain"
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_rename_exception_after_commit_reports_that_the_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._rename_noreplace

    def commit_then_fail(*args):
        original(*args)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(create_module, "_rename_noreplace", commit_then_fail)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-commit-uncertain"
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_detached_stage_after_publication_attempt_reports_uncertain_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    moved = tmp_path / "moved"
    original = create_module._rename_noreplace

    def commit_move_then_fail(*args):
        original(*args)
        target.rename(moved)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(create_module, "_rename_noreplace", commit_move_then_fail)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-commit-uncertain"
    assert "publication may have occurred" in raised.value.message
    assert not target.exists()
    assert inspect_project(moved).ok


def test_publication_capability_error_remains_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"

    def unavailable(*args):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "Atomic no-replace rename is unavailable.",
        )

    monkeypatch.setattr(create_module, "_rename_noreplace", unavailable)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-safety-unavailable"
    assert ".autoform-new-* stage may remain" in raised.value.message
    assert not target.exists()
    assert len(list(tmp_path.glob(".autoform-new-*"))) == 1


def test_unsupported_rename_flag_uses_capability_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRename:
        argtypes = None
        restype = None

        def __call__(self, *args):
            return -1

    class Libc:
        renameat2 = FailingRename()

    monkeypatch.setattr(create_module.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(create_module.ctypes, "get_errno", lambda: errno.EINVAL)

    with pytest.raises(ProjectCreateError) as raised:
        create_module._rename_noreplace(3, "stage", 3, "target")

    assert raised.value.code == "project-create-safety-unavailable"


def test_missing_native_noreplace_operation_uses_capability_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        create_module.ctypes,
        "CDLL",
        lambda *args, **kwargs: object(),
    )

    assert create_module._noreplace_function() is None
    with pytest.raises(ProjectCreateError) as raised:
        create_module._rename_noreplace(3, "stage", 3, "target")
    assert raised.value.code == "project-create-safety-unavailable"


def test_open_parent_close_after_close_does_not_retry_or_leak_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_close = create_module.os.close
    original_fstat = create_module.os.fstat
    close_attempts: dict[int, int] = {}
    failed = False

    def close_after_close(descriptor: int) -> None:
        nonlocal failed
        close_attempts[descriptor] = close_attempts.get(descriptor, 0) + 1
        original_close(descriptor)
        if not failed:
            failed = True
            raise OSError("injected close-after-close failure")

    monkeypatch.setattr(create_module.os, "close", close_after_close)

    with pytest.raises(ProjectCreateError) as raised:
        create_module._open_parent(tmp_path)

    assert raised.value.code == "project-path-is-symlink"
    assert len(close_attempts) == 2
    assert all(attempts == 1 for attempts in close_attempts.values())
    for descriptor in close_attempts:
        with pytest.raises(OSError) as closed:
            original_fstat(descriptor)
        assert closed.value.errno == errno.EBADF


def test_late_target_race_remains_distinguishable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"

    def lose_race(*args):
        target.mkdir()
        (target / "KEEP").write_text("keep\n", encoding="utf-8")
        raise FileExistsError

    monkeypatch.setattr(create_module, "_rename_noreplace", lose_race)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-target-exists"
    assert ".autoform-new-* stage may remain" in raised.value.message
    assert (target / "KEEP").read_text(encoding="utf-8") == "keep\n"
    assert len(list(tmp_path.glob(".autoform-new-*"))) == 1


def test_workspace_substitution_fails_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._validate_staged_project

    def substitute(*args, **kwargs) -> None:
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        moved = stage.with_name(f"{stage.name}-owned")
        stage.rename(moved)
        stage.mkdir(mode=0o700)
        (stage / "FOREIGN").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(create_module, "_validate_staged_project", substitute)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)
    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    assert any(path.name == "FOREIGN" for path in tmp_path.rglob("FOREIGN"))


def test_stage_path_substitution_never_writes_to_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "KEEP").write_text("keep\n", encoding="utf-8")
    original = create_module._materialize_project

    def substitute(stage_descriptor, plan) -> None:
        stage = next(tmp_path.glob(".autoform-new-*"))
        stage.rename(stage.with_name(f"{stage.name}-owned"))
        stage.symlink_to(victim, target_is_directory=True)
        original(stage_descriptor, plan)

    monkeypatch.setattr(create_module, "_materialize_project", substitute)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    assert (victim / "KEEP").read_text(encoding="utf-8") == "keep\n"
    assert sorted(path.name for path in victim.iterdir()) == ["KEEP"]


def test_stage_open_failure_preserves_the_owned_empty_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._open_stage

    def fail_first_open(parent_descriptor: int, stage_name: str) -> int:
        if stage_name.startswith(".autoform-new-"):
            raise OSError("injected stage open failure")
        return original(parent_descriptor, stage_name)

    monkeypatch.setattr(create_module, "_open_stage", fail_first_open)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert not list(stages[0].iterdir())


def test_failure_path_never_attempts_recursive_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"

    def fail(*args, **kwargs):
        raise OSError("injected")

    def forbidden(*args, **kwargs):
        raise AssertionError("project creation attempted destructive cleanup")

    monkeypatch.setattr(create_module, "_validate_staged_project", fail)
    monkeypatch.setattr(create_module.os, "unlink", forbidden)
    monkeypatch.setattr(create_module.os, "rmdir", forbidden)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not target.exists()


def test_failure_cleanup_never_recurses_into_a_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "KEEP").write_text("keep\n", encoding="utf-8")
    original = create_module._validate_staged_project

    def substitute(*args, **kwargs):
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        (stage / "blueprint").rename(stage / "owned-blueprint")
        victim.rename(stage / "blueprint")
        raise OSError("injected")

    monkeypatch.setattr(create_module, "_validate_staged_project", substitute)
    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert (stages[0] / "blueprint/KEEP").read_text(encoding="utf-8") == "keep\n"


def test_hard_linked_planned_file_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    alias = tmp_path / "alias"
    original = create_module._materialize_project

    def add_alias(*args, **kwargs):
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        os.link(stage / "lean-toolchain", alias)

    monkeypatch.setattr(create_module, "_materialize_project", add_alias)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    assert alias.stat().st_nlink == 2


def test_noncanonical_generated_directory_mode_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._materialize_project

    def make_world_writable(*args, **kwargs):
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        (stage / "blueprint").chmod(0o777)

    monkeypatch.setattr(create_module, "_materialize_project", make_world_writable)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not target.exists()
    stage = next(tmp_path.glob(".autoform-new-*"))
    assert stat.S_IMODE((stage / "blueprint").stat().st_mode) == 0o777


def test_mutation_after_validation_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._validate_staged_project

    def mutate(*args, **kwargs):
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        (stage / "lean-toolchain").write_text("mutated\n", encoding="utf-8")

    monkeypatch.setattr(create_module, "_validate_staged_project", mutate)

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, package="Project", release_id=_RELEASE)

    assert raised.value.code == "project-create-failed"
    assert not target.exists()


def test_verifier_opens_regular_files_nonblocking(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def capture(name, flags, *, dir_fd):
        captured.update(name=name, flags=flags, dir_fd=dir_fd)
        return 17

    monkeypatch.setattr(create_module.os, "open", capture)

    assert create_module._open_planned_file(9, "lean-toolchain") == 17
    assert captured == {
        "name": "lean-toolchain",
        "flags": os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        "dir_fd": 9,
    }


def test_concurrent_creation_has_exactly_one_winner(tmp_path: Path) -> None:
    target = tmp_path / "project"
    barrier = threading.Barrier(2)
    results: list[str] = []

    def run() -> None:
        barrier.wait(timeout=10)
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
    assert not list(tmp_path.glob(".autoform-new-*"))


@pytest.mark.parametrize(
    "arguments, code",
    [
        (["project", "new", "--json"], "project-target-invalid"),
        (["project", "new", "project", "--release", _RELEASE, "--json"], "project-name-invalid"),
        (["project", "new", "project", "--package", "Project", "--json"], "project-release-unknown"),
    ],
)
def test_cli_missing_creation_options_are_json(arguments: list[str], code: str, capsys) -> None:
    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == code
    assert captured.err == ""


def test_cli_json_is_stable_and_path_free(tmp_path: Path, capsys) -> None:
    target = tmp_path / "project"
    assert (
        main(
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
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["target"] == "project"
    assert str(tmp_path) not in captured.out
    assert captured.err == ""

    duplicate = tmp_path / "project"
    assert (
        main(
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
        )
        == 1
    )
    failed = capsys.readouterr()
    assert json.loads(failed.out)["error"]["code"] == "project-target-exists"
    assert failed.err == ""


def test_cli_threads_the_explicit_workflow_pin(tmp_path: Path, capsys) -> None:
    target = tmp_path / "Pinned"
    source = "https://example.test/owner/autoform.git"
    revision = "5" * 40

    assert (
        main(
            [
                "project",
                "new",
                os.fspath(target),
                "--package",
                "Pinned",
                "--release",
                _RELEASE,
                "--autoform-source",
                source,
                "--autoform-ref",
                revision,
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["workflows_pinned"] is True
    workflow = (target / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f'AUTOFORM_SOURCE: "{source}"' in workflow
    assert f'AUTOFORM_REF: "{revision}"' in workflow
