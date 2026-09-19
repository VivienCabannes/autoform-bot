from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.project import (
    ProjectCreateError,
    ProjectRepairConflict,
    ProjectRepairError,
    create_project,
    repair_project,
)
from autoform_cli.project import repair as repair_module

_RELEASE = "lean-v4.32.2-mathlib-v4.32.2"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    create_project(root, package="Project", release_id=_RELEASE)
    return root


def _repair(target: str | Path, **kwargs):
    options = {"title": "Project", "repository_url": ""}
    options.update(kwargs)
    return repair_project(target, **options)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_repairs_only_missing_overlay_files_and_preserves_existing_bytes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    authored = b"# Authored landing page\n"
    (root / "README.md").write_bytes(authored)
    (root / "mkdocs.yml").unlink()
    (root / "blueprint/coverage/README.md").unlink()
    before = _files(root)

    result = _repair(root)

    assert result.planned == ("blueprint/coverage/README.md", "mkdocs.yml")
    assert result.written == result.planned
    assert (root / "README.md").read_bytes() == authored
    after = _files(root)
    for path, content in before.items():
        assert after[path] == content
    assert (root / "mkdocs.yml").is_file()
    assert (root / "blueprint/coverage/README.md").is_file()


def test_dry_run_reports_exact_plan_without_writing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    before = _files(root)

    result = _repair(root, dry_run=True)

    assert result.dry_run
    assert result.planned == ("mkdocs.yml",)
    assert result.written == ()
    assert _files(root) == before
    assert not (root / "mkdocs.yml").exists()


def test_second_repair_is_a_noop(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    first = _repair(root)
    after_first = _files(root)
    second = _repair(root)

    assert first.written == ("mkdocs.yml",)
    assert second.planned == ()
    assert second.written == ()
    assert _files(root) == after_first


def test_aggregate_conflicts_produce_zero_writes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    shutil.rmtree(root / "theme")
    (root / "theme").write_bytes(b"authored blocker\n")
    (root / "mkdocs.yml").unlink()
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-conflict"
    assert {conflict.code for conflict in raised.value.conflicts} == {
        "project-repair-parent-not-directory"
    }
    assert _files(root) == before
    assert not (root / "mkdocs.yml").exists()


def test_nested_target_is_rejected_without_writes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    nested = root / "src"
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(nested)

    assert raised.value.conflicts[0].code == "project-repair-target-invalid"
    assert _files(root) == before


def test_missing_managed_parent_is_a_zero_write_conflict(tmp_path: Path) -> None:
    root = _project(tmp_path)
    shutil.rmtree(root / "blueprint/coverage")
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert any(
        conflict.code == "project-repair-parent-missing"
        and conflict.path == "blueprint/coverage"
        for conflict in raised.value.conflicts
    )
    assert _files(root) == before
    assert not (root / "blueprint/coverage").exists()


def test_malformed_or_unsupported_project_produces_zero_writes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lean-toolchain").write_text("leanprover/lean4:v0.0.0\n", encoding="utf-8")
    (root / "mkdocs.yml").unlink()
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert any(
        conflict.code == "project-repair-release-indeterminate"
        for conflict in raised.value.conflicts
    )
    assert _files(root) == before


def test_existing_managed_files_are_authoritative(tmp_path: Path) -> None:
    root = _project(tmp_path)
    authored = b"not generated yaml, but deliberately preserved\n"
    (root / "mkdocs.yml").write_bytes(authored)

    result = _repair(root)

    assert result.planned == ()
    assert "mkdocs.yml" in result.preserved
    assert (root / "mkdocs.yml").read_bytes() == authored


def test_concurrent_repairs_serialize_without_overwriting(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(_repair(root))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sum(result.written == ("mkdocs.yml",) for result in results) == 1
    assert sum(result.planned == () for result in results) == 1
    assert (root / "mkdocs.yml").is_file()


def test_different_concurrent_winner_is_retained_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module._rename_noreplace

    def competing(source_parent, source, target_parent, target):
        if target == "mkdocs.yml":
            descriptor = repair_module.os.open(
                target,
                repair_module.os.O_WRONLY
                | repair_module.os.O_CREAT
                | repair_module.os.O_EXCL,
                0o644,
                dir_fd=target_parent,
            )
            try:
                repair_module.os.write(descriptor, b"concurrent authored content\n")
            finally:
                repair_module.os.close(descriptor)
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", competing)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert (root / "mkdocs.yml").read_bytes() == b"concurrent authored content\n"
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_repair_does_not_discover_git_provenance_or_run_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("repair invoked a forbidden external operation")

    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    result = _repair(root)
    assert result.written == ("mkdocs.yml",)


def test_root_substitution_after_planning_is_a_zero_write_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module._plan

    def substitute(*args, **kwargs):
        plan = original(*args, **kwargs)
        moved = root.with_name("original-project")
        root.rename(moved)
        root.mkdir()
        shutil.copy2(moved / "lakefile.toml", root / "lakefile.toml")
        shutil.copy2(moved / "lean-toolchain", root / "lean-toolchain")
        return plan

    monkeypatch.setattr(repair_module, "_plan", substitute)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-race-conflict"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()


def test_parent_substitution_at_publish_retains_the_detached_file_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    detached = tmp_path / "detached-blueprint"
    original = repair_module._rename_noreplace

    def substitute(source_parent, source, target_parent, target):
        (root / "blueprint").rename(detached)
        (root / "blueprint/coverage").mkdir(parents=True)
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", substitute)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("blueprint/coverage/README.md",)
    assert (detached / "coverage/README.md").is_file()
    assert not (root / "blueprint/coverage/README.md").exists()


def test_root_open_failure_uses_repair_error_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _project(tmp_path)

    def unavailable(*args, **kwargs):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "The platform cannot traverse the target safely.",
        )

    monkeypatch.setattr(repair_module, "_open_parent", unavailable)

    assert main(["project", "repair", str(root), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == "autoform-project-repair/v1"
    assert result["error"]["code"] == "project-repair-safety-unavailable"
    assert result["error"]["conflicts"][0]["code"] == (
        "project-repair-safety-unavailable"
    )


def test_preflight_io_failure_uses_repair_error_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _project(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("injected preflight failure")

    monkeypatch.setattr(repair_module, "_descriptor_identity", fail)

    assert main(["project", "repair", str(root), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == "autoform-project-repair/v1"
    assert result["error"]["code"] == "project-repair-failed"
    assert result["error"]["conflicts"][0]["code"] == "project-repair-io-failed"
    assert result["written"] == []


def test_fifo_concurrent_winner_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module._rename_noreplace

    def competing(source_parent, source, target_parent, target):
        if target == "mkdocs.yml":
            repair_module.os.mkfifo(target, dir_fd=target_parent)
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", competing)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert (root / "mkdocs.yml").is_fifo()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_configuration_change_during_staging_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module.os.write
    changed = False

    def mutate_configuration(descriptor, content):
        nonlocal changed
        count = original(descriptor, content)
        if not changed:
            changed = True
            (root / "lean-toolchain").write_text(
                "leanprover/lean4:v0.0.0\n", encoding="utf-8"
            )
        return count

    monkeypatch.setattr(repair_module.os, "write", mutate_configuration)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_staging_write_failure_retains_temporary_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    def fail_write(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(repair_module.os, "write", fail_write)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_retained_temporary_descriptor_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    temporary_descriptor = None

    def fail_write(descriptor, *args, **kwargs):
        nonlocal temporary_descriptor
        temporary_descriptor = descriptor
        raise OSError("injected write failure")

    monkeypatch.setattr(repair_module.os, "write", fail_write)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert temporary_descriptor is not None
    with pytest.raises(OSError) as closed:
        repair_module.os.fstat(temporary_descriptor)
    assert closed.value.errno == errno.EBADF
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_child_descriptor_is_closed_when_device_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    root_descriptor = repair_module._open_root(root)
    original_fstat = repair_module.os.fstat
    child_descriptor = None

    def fail_child(descriptor):
        nonlocal child_descriptor
        if descriptor != root_descriptor:
            child_descriptor = descriptor
            raise OSError("injected child metadata failure")
        return original_fstat(descriptor)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(repair_module.os, "fstat", fail_child)
            with pytest.raises(OSError):
                repair_module._managed_path_state(
                    root_descriptor, "blueprint/coverage/README.md"
                )
        assert child_descriptor is not None
        with pytest.raises(OSError) as closed:
            original_fstat(child_descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        repair_module.os.close(root_descriptor)


def test_managed_path_state_preserves_fstat_error_when_child_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    root_descriptor = repair_module._open_root(root)
    original_open = repair_module.os.open
    original_close = repair_module.os.close
    original_fstat = repair_module.os.fstat
    child_descriptor = None
    close_attempts = 0

    def track_open(name, *args, **kwargs):
        nonlocal child_descriptor
        descriptor = original_open(name, *args, **kwargs)
        if name == "blueprint":
            child_descriptor = descriptor
        return descriptor

    def fail_child_fstat(descriptor):
        if descriptor == child_descriptor:
            raise OSError("injected metadata finding")
        return original_fstat(descriptor)

    def fail_child_close(descriptor):
        nonlocal close_attempts
        if descriptor == child_descriptor:
            close_attempts += 1
            original_close(descriptor)
            raise OSError("injected close-after-close failure")
        return original_close(descriptor)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(repair_module.os, "open", track_open)
            patch.setattr(repair_module.os, "fstat", fail_child_fstat)
            patch.setattr(repair_module.os, "close", fail_child_close)
            with pytest.raises(OSError, match="metadata finding"):
                repair_module._managed_path_state(
                    root_descriptor, "blueprint/coverage/README.md"
                )
        assert child_descriptor is not None
        assert close_attempts == 1
        with pytest.raises(OSError) as closed:
            original_fstat(child_descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        original_close(root_descriptor)


def test_directory_transfer_close_failure_closes_each_descriptor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    parent_descriptor = repair_module._open_root(root)
    owner = repair_module._OwnedDescriptor(parent_descriptor)
    original_open = repair_module.os.open
    original_close = repair_module.os.close
    original_fstat = repair_module.os.fstat
    child_descriptor = None
    attempts: dict[int, int] = {}

    def track_open(*args, **kwargs):
        nonlocal child_descriptor
        descriptor = original_open(*args, **kwargs)
        child_descriptor = descriptor
        return descriptor

    def fail_parent_close(descriptor):
        attempts[descriptor] = attempts.get(descriptor, 0) + 1
        original_close(descriptor)
        if descriptor == parent_descriptor:
            raise OSError("injected close-after-close failure")

    with monkeypatch.context() as patch:
        patch.setattr(repair_module.os, "open", track_open)
        patch.setattr(repair_module.os, "close", fail_parent_close)
        with pytest.raises(ProjectRepairError) as raised:
            repair_module._open_existing_directory(
                owner,
                "blueprint",
                "blueprint",
                expected_device=original_fstat(parent_descriptor).st_dev,
            )

    assert raised.value.conflicts[0].code == "project-repair-close-failed"
    assert child_descriptor is not None
    assert attempts[parent_descriptor] == 1
    assert attempts[child_descriptor] == 1
    for descriptor in (parent_descriptor, child_descriptor):
        with pytest.raises(OSError) as closed:
            original_fstat(descriptor)
        assert closed.value.errno == errno.EBADF


def test_recovery_scan_preserves_finding_when_close_after_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    root_descriptor = repair_module._open_root(root)
    original_dup = repair_module.os.dup
    original_close = repair_module.os.close
    duplicate = None
    close_attempts = 0
    finding = ProjectRepairConflict(
        "project-repair-recovery-required",
        "injected recovery finding",
        ".README.md.autoform-repair-0123456789abcdef",
    )

    def track_dup(descriptor):
        nonlocal duplicate
        duplicate = original_dup(descriptor)
        return duplicate

    def fail_scan(*args, **kwargs):
        raise ProjectRepairError((finding,), code="project-repair-recovery-required")

    def fail_duplicate_close(descriptor):
        nonlocal close_attempts
        if descriptor == duplicate:
            close_attempts += 1
            original_close(descriptor)
            raise OSError("injected close-after-close failure")
        return original_close(descriptor)

    item = repair_module._PlannedFile("README.md", b"content\n", 0o644)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(repair_module.os, "dup", track_dup)
            patch.setattr(repair_module, "_directory_entries", fail_scan)
            patch.setattr(repair_module.os, "close", fail_duplicate_close)
            conflicts = repair_module._find_recovery_conflicts(
                root_descriptor, (item,)
            )
        assert close_attempts == 1
        assert finding in conflicts
        assert any(
            conflict.code == "project-repair-close-failed"
            for conflict in conflicts
        )
    finally:
        original_close(root_descriptor)


def test_concurrent_result_closes_descriptor_on_non_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    target = root / "mkdocs.yml"
    item = repair_module._PlannedFile(
        "mkdocs.yml",
        target.read_bytes(),
        stat.S_IMODE(target.stat().st_mode),
    )
    root_descriptor = repair_module._open_root(root)
    original_fstat = repair_module.os.fstat
    winner_descriptor = None

    def interrupt(descriptor):
        nonlocal winner_descriptor
        winner_descriptor = descriptor
        raise KeyboardInterrupt

    try:
        with monkeypatch.context() as patch:
            patch.setattr(repair_module.os, "fstat", interrupt)
            with pytest.raises(KeyboardInterrupt):
                repair_module._concurrent_result(root_descriptor, "mkdocs.yml", item)
        assert winner_descriptor is not None
        with pytest.raises(OSError) as closed:
            original_fstat(winner_descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        repair_module.os.close(root_descriptor)


def test_winner_close_failure_preserves_validation_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    target = root / "mkdocs.yml"
    target.unlink()
    original_manifest = repair_module._require_file_manifest
    original_close = repair_module.os.close
    winner_descriptor = None
    close_failed = False

    def publish_competitor(source_parent, source, target_parent, name):
        target.write_bytes((root / source).read_bytes())
        target.chmod(0o644)
        raise FileExistsError

    def fail_winner_validation(parent, name, descriptor, identity, item):
        nonlocal winner_descriptor
        if name == "mkdocs.yml":
            winner_descriptor = descriptor
            raise OSError("injected winner validation failure")
        return original_manifest(parent, name, descriptor, identity, item)

    def fail_winner_close(descriptor):
        nonlocal close_failed
        if descriptor == winner_descriptor and not close_failed:
            close_failed = True
            original_close(descriptor)
            raise OSError("injected winner close failure")
        return original_close(descriptor)

    monkeypatch.setattr(repair_module, "_rename_noreplace", publish_competitor)
    monkeypatch.setattr(repair_module, "_require_file_manifest", fail_winner_validation)
    monkeypatch.setattr(repair_module.os, "close", fail_winner_close)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert close_failed
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert [conflict.code for conflict in raised.value.conflicts] == [
        "project-repair-race-conflict",
        "project-repair-close-failed",
        "project-repair-recovery-required",
    ]
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name
    assert target.is_file()


def test_render_failure_uses_repair_error_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(repair_module, "_scaffold_plan", fail)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root, dry_run=True)
    assert raised.value.code == "project-repair-failed"
    assert raised.value.conflicts[0].code == "project-repair-render-failed"


def test_unsupported_atomic_publish_uses_repair_error_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    def unavailable(*args, **kwargs):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "Atomic no-replace publication is unavailable.",
        )

    monkeypatch.setattr(repair_module, "_rename_noreplace", unavailable)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_unsupported_publication_is_rejected_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    monkeypatch.setattr(repair_module, "_noreplace_function", lambda: None)

    dry_run = _repair(root, dry_run=True)
    assert dry_run.planned == ("mkdocs.yml",)

    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-safety-unavailable"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()
    assert not list(root.rglob(".*.autoform-repair-*"))


def test_post_publish_fsync_failure_reports_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module.os.fsync

    def fail_directory(descriptor: int) -> None:
        if stat.S_ISDIR(repair_module.os.fstat(descriptor).st_mode):
            raise OSError("injected")
        original(descriptor)

    monkeypatch.setattr(repair_module.os, "fsync", fail_directory)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)
    assert raised.value.code == "project-repair-failed"
    assert raised.value.written == ("mkdocs.yml",)
    assert (root / "mkdocs.yml").is_file()


def test_post_publish_close_failure_reports_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original_write = repair_module.os.write
    original_close = repair_module.os.close
    staged_descriptor = None
    close_failed = False

    def track_write(descriptor, content):
        nonlocal staged_descriptor
        staged_descriptor = descriptor
        return original_write(descriptor, content)

    def fail_staged_close(descriptor):
        nonlocal close_failed
        if descriptor == staged_descriptor and not close_failed:
            close_failed = True
            original_close(descriptor)
            raise OSError("injected close failure")
        return original_close(descriptor)

    monkeypatch.setattr(repair_module.os, "write", track_write)
    monkeypatch.setattr(repair_module.os, "close", fail_staged_close)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert close_failed
    assert raised.value.code == "project-repair-failed"
    assert raised.value.written == ("mkdocs.yml",)
    assert (root / "mkdocs.yml").is_file()


def test_close_failure_preserves_pending_recovery_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    original_rename = repair_module._rename_noreplace
    original_write = repair_module.os.write
    original_close = repair_module.os.close
    staged_descriptor = None
    close_failed = False

    def make_unsafe(source_parent, source, target_parent, target):
        result = original_rename(source_parent, source, target_parent, target)
        root.chmod(0o777)
        return result

    def track_write(descriptor, content):
        nonlocal staged_descriptor
        staged_descriptor = descriptor
        return original_write(descriptor, content)

    def fail_staged_close(descriptor):
        nonlocal close_failed
        if descriptor == staged_descriptor and not close_failed:
            close_failed = True
            original_close(descriptor)
            raise OSError("injected close failure")
        return original_close(descriptor)

    monkeypatch.setattr(repair_module, "_rename_noreplace", make_unsafe)
    monkeypatch.setattr(repair_module.os, "write", track_write)
    monkeypatch.setattr(repair_module.os, "close", fail_staged_close)
    try:
        with pytest.raises(ProjectRepairError) as raised:
            repair_project(root)
        assert close_failed
        assert raised.value.code == "project-repair-recovery-required"
        assert raised.value.written == ("blueprint/coverage/README.md",)
        assert {conflict.code for conflict in raised.value.conflicts} == {
            "project-repair-recovery-required",
            "project-repair-close-failed",
            "project-repair-parent-unsafe",
        }
        assert missing.is_file()
    finally:
        root.chmod(0o755)


def test_root_close_failure_reports_files_already_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original_open_root = repair_module._open_root
    original_close = repair_module.os.close
    root_descriptor = None
    close_failed = False

    def capture_root(path):
        nonlocal root_descriptor
        root_descriptor = original_open_root(path)
        return root_descriptor

    def fail_root_close(descriptor):
        nonlocal close_failed
        if descriptor == root_descriptor and not close_failed:
            close_failed = True
            original_close(descriptor)
            raise OSError("injected root close failure")
        return original_close(descriptor)

    monkeypatch.setattr(repair_module, "_open_root", capture_root)
    monkeypatch.setattr(repair_module.os, "close", fail_root_close)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert close_failed
    assert raised.value.code == "project-repair-failed"
    assert raised.value.written == ("mkdocs.yml",)
    assert raised.value.conflicts[-1].code == "project-repair-close-failed"
    assert (root / "mkdocs.yml").is_file()


def test_dry_run_rejects_same_unsafe_root_as_apply(tmp_path: Path) -> None:
    root = _project(tmp_path)
    root.chmod(0o777)
    try:
        with pytest.raises(ProjectRepairError) as raised:
            _repair(root, dry_run=True)
        assert any(
            conflict.code == "project-repair-parent-unsafe"
            for conflict in raised.value.conflicts
        )
    finally:
        root.chmod(0o755)


def test_cli_json_reports_dry_run_and_conflicts(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    assert main(
        [
            "project",
            "repair",
            str(root),
            "--title",
            "Project",
            "--repository-url",
            "",
            "--dry-run",
            "--json",
        ]
    ) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["schema"] == "autoform-project-repair/v1"
    assert dry_run["planned"] == ["mkdocs.yml"]
    assert dry_run["written"] == []

    (root / "blueprint/roadmap/README.md").unlink()
    (root / "blueprint/roadmap").rmdir()
    (root / "blueprint/roadmap").write_bytes(b"blocker\n")
    assert main(
        [
            "project",
            "repair",
            str(root),
            "--title",
            "Project",
            "--repository-url",
            "",
            "--json",
        ]
    ) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["error"]["code"] == "project-repair-conflict"
    assert failed["written"] == []


def test_cli_text_error_reports_files_already_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _project(tmp_path)
    (root / "blueprint/coverage/README.md").unlink()
    (root / "mkdocs.yml").unlink()
    original = repair_module._publish

    def fail_second(
        root, root_descriptor, root_identity, item, inspection, observations
    ):
        if item.path == "mkdocs.yml":
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "injected-repair-failure",
                        "Injected failure after an earlier publication.",
                        item.path,
                    ),
                ),
                code="project-repair-failed",
            )
        return original(
            root,
            root_descriptor,
            root_identity,
            item,
            inspection,
            observations,
        )

    monkeypatch.setattr(repair_module, "_publish", fail_second)
    assert (
        main(
            [
                "project",
                "repair",
                str(root),
                "--title",
                "Project",
                "--repository-url",
                "",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "files already or possibly published:" in captured.err
    assert "blueprint/coverage/README.md" in captured.err


def test_missing_parameterized_file_requires_exact_inputs(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.written == ()
    assert [
        (conflict.code, conflict.path) for conflict in raised.value.conflicts
    ] == [("project-repair-input-required", "mkdocs.yml")]
    assert not (root / "mkdocs.yml").exists()


def test_explicit_empty_repository_url_is_not_omission(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    with pytest.raises(ProjectRepairError):
        repair_project(root, title="Project")
    result = repair_project(root, title="Project", repository_url="")

    assert result.written == ("mkdocs.yml",)
    assert b'repo_url: ""' in (root / "mkdocs.yml").read_bytes()


def test_unpinned_project_can_repair_static_files_without_workflow_inputs(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()

    result = repair_project(root)

    assert result.written == ("blueprint/coverage/README.md",)
    assert not (root / ".github").exists()


def test_partial_workflow_state_requires_explicit_provenance(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workflows = root / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "autoform-verify.yml").write_text("authored\n", encoding="utf-8")

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.written == ()
    assert any(
        conflict.code == "project-repair-input-required"
        and conflict.path == ".github/workflows/blueprint-pages.yml"
        for conflict in raised.value.conflicts
    )
    assert not (root / ".github/autoform_audit.py").exists()


@pytest.mark.parametrize("source,ref", [("", ""), ("", "0" * 40)])
def test_blank_workflow_provenance_is_rejected(
    tmp_path: Path, source: str, ref: str
) -> None:
    root = _project(tmp_path)

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root, autoform_source=source, autoform_ref=ref)

    assert raised.value.code == "project-repair-input-invalid"
    assert raised.value.written == ()


def test_reserved_temporary_file_requires_manual_recovery(tmp_path: Path) -> None:
    root = _project(tmp_path)
    orphan = root / ".mkdocs.yml.autoform-repair-0123456789abcdef"
    orphan.write_bytes(b"unverified\n")
    before = _files(root)

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.written == ()
    assert any(
        conflict.code == "project-repair-recovery-required"
        and conflict.path == orphan.name
        for conflict in raised.value.conflicts
    )
    assert _files(root) == before


def test_recovery_directory_scan_stops_at_the_entry_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "directory"
    root.mkdir()
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    consumed: list[str] = []

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class Scan:
        def __enter__(self):
            def entries():
                for index in range(10):
                    name = str(index)
                    consumed.append(name)
                    yield Entry(name)

            return entries()

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(repair_module, "_MAX_DIRECTORY_ENTRIES", 2)
    monkeypatch.setattr(repair_module.os, "scandir", lambda _descriptor: Scan())
    try:
        with pytest.raises(ProjectRepairError) as raised:
            repair_module._directory_entries(descriptor, ".")
    finally:
        os.close(descriptor)

    assert raised.value.conflicts[0].code == "project-repair-directory-too-large"
    assert consumed == ["0", "1", "2"]


def test_ancestor_symlink_target_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(root.parent, target_is_directory=True)

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(alias / root.name)

    assert raised.value.written == ()
    assert raised.value.conflicts[0].code == "project-repair-target-invalid"


def test_root_substitution_inside_publish_retains_detached_file_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    detached = tmp_path / "detached-project"
    original = repair_module._rename_noreplace

    def substitute(source_parent, source, target_parent, target):
        root.rename(detached)
        root.mkdir()
        shutil.copy2(detached / "lakefile.toml", root / "lakefile.toml")
        shutil.copy2(detached / "lean-toolchain", root / "lean-toolchain")
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", substitute)
    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("blueprint/coverage/README.md",)
    assert (detached / "blueprint/coverage/README.md").is_file()
    assert not (root / "blueprint/coverage/README.md").exists()


def test_root_permission_change_at_publish_retains_file_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    original = repair_module._rename_noreplace
    original_unlink = repair_module.os.unlink
    published = False

    def make_unsafe(source_parent, source, target_parent, target):
        nonlocal published
        result = original(source_parent, source, target_parent, target)
        published = True
        root.chmod(0o777)
        return result

    def reject_published_unlink(path, *args, **kwargs):
        if published and path == "README.md" and kwargs.get("dir_fd") is not None:
            raise AssertionError("published recovery path must not be unlinked by name")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(repair_module, "_rename_noreplace", make_unsafe)
    monkeypatch.setattr(repair_module.os, "unlink", reject_published_unlink)
    try:
        with pytest.raises(ProjectRepairError) as raised:
            repair_project(root)
        assert raised.value.code == "project-repair-recovery-required"
        assert raised.value.written == ("blueprint/coverage/README.md",)
        assert missing.is_file()
    finally:
        root.chmod(0o755)


def test_temporary_content_mutation_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()
    original = repair_module._rename_noreplace

    def mutate(source_parent, source, target_parent, target):
        descriptor = repair_module.os.open(
            source, repair_module.os.O_WRONLY | repair_module.os.O_TRUNC, dir_fd=source_parent
        )
        try:
            repair_module.os.write(descriptor, b"foreign bytes\n")
        finally:
            repair_module.os.close(descriptor)
        return original(source_parent, source, target_parent, target)

    monkeypatch.setattr(repair_module, "_rename_noreplace", mutate)
    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("blueprint/coverage/README.md",)
    assert missing.read_bytes() == b"foreign bytes\n"


def test_concurrent_winner_replacement_is_not_reported_as_converged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    missing = root / "blueprint/coverage/README.md"
    expected = missing.read_bytes()
    missing.unlink()
    original_rename = repair_module._rename_noreplace
    original_result = repair_module._concurrent_result

    def competing(source_parent, source, target_parent, target):
        descriptor = repair_module.os.open(
            target,
            repair_module.os.O_WRONLY
            | repair_module.os.O_CREAT
            | repair_module.os.O_EXCL,
            0o644,
            dir_fd=target_parent,
        )
        try:
            repair_module.os.write(descriptor, expected)
        finally:
            repair_module.os.close(descriptor)
        return original_rename(source_parent, source, target_parent, target)

    def replace_after_read(parent_descriptor, name, item):
        result = original_result(parent_descriptor, name, item)
        repair_module.os.unlink(name, dir_fd=parent_descriptor)
        descriptor = repair_module.os.open(
            name,
            repair_module.os.O_WRONLY
            | repair_module.os.O_CREAT
            | repair_module.os.O_EXCL,
            0o644,
            dir_fd=parent_descriptor,
        )
        try:
            repair_module.os.write(descriptor, b"foreign bytes\n")
        finally:
            repair_module.os.close(descriptor)
        return result

    monkeypatch.setattr(repair_module, "_rename_noreplace", competing)
    monkeypatch.setattr(repair_module, "_concurrent_result", replace_after_read)
    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert missing.read_bytes() == b"foreign bytes\n"
    temporary, = (root / "blueprint/coverage").glob(
        ".README.md.autoform-repair-*"
    )
    assert raised.value.conflicts[-1].path == temporary.relative_to(root).as_posix()


@pytest.mark.parametrize("mutation", ["delete", "replace", "modify"])
def test_preserved_file_mutation_during_publication_is_not_success(
    mutation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    preserved = root / "README.md"
    (root / "mkdocs.yml").unlink()
    original = repair_module._rename_noreplace

    def mutate_then_publish(*args):
        if mutation == "delete":
            preserved.unlink()
        elif mutation == "replace":
            replacement = root / "README.replacement"
            replacement.write_bytes(b"replacement\n")
            replacement.replace(preserved)
        else:
            preserved.write_bytes(b"modified in place\n")
        return original(*args)

    monkeypatch.setattr(repair_module, "_rename_noreplace", mutate_then_publish)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("mkdocs.yml",)
    assert any(
        conflict.code == "project-repair-race-conflict"
        and conflict.path == "README.md"
        for conflict in raised.value.conflicts
    )
    assert (root / "mkdocs.yml").is_file()


@pytest.mark.parametrize("parent_exists", [False, True])
def test_omitted_workflow_appearance_is_not_success(
    parent_exists: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "blueprint/coverage/README.md").unlink()
    workflows = root / ".github/workflows"
    if parent_exists:
        workflows.mkdir(parents=True)
    original = repair_module._rename_noreplace

    def add_workflow_then_publish(*args):
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / "autoform-verify.yml").write_text(
            "concurrent workflow\n", encoding="utf-8"
        )
        return original(*args)

    monkeypatch.setattr(repair_module, "_rename_noreplace", add_workflow_then_publish)
    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("blueprint/coverage/README.md",)
    assert any(
        conflict.code == "project-repair-race-conflict"
        and conflict.path == ".github/workflows/autoform-verify.yml"
        for conflict in raised.value.conflicts
    )


def test_first_publication_change_during_second_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    first = root / "blueprint/coverage/README.md"
    first.unlink()
    (root / "mkdocs.yml").unlink()
    original = repair_module._rename_noreplace
    calls = 0

    def delete_first_during_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            first.unlink()
        return original(*args)

    monkeypatch.setattr(repair_module, "_rename_noreplace", delete_first_during_second)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == (
        "blueprint/coverage/README.md",
        "mkdocs.yml",
    )
    assert any(
        conflict.code == "project-repair-race-conflict"
        and conflict.path == "blueprint/coverage/README.md"
        for conflict in raised.value.conflicts
    )
    assert not first.exists()
    assert (root / "mkdocs.yml").is_file()


@pytest.mark.parametrize("dry_run", [False, True])
def test_final_config_drift_blocks_noop_and_dry_run_success(
    dry_run: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    if dry_run:
        (root / "blueprint/coverage/README.md").unlink()
    original = repair_module._plan

    def mutate_after_plan(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / "lean-toolchain").write_text(
            "leanprover/lean4:v0.0.0\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(repair_module, "_plan", mutate_after_plan)
    with pytest.raises(ProjectRepairError) as raised:
        repair_project(root, dry_run=dry_run)

    assert raised.value.code == "project-repair-race-conflict"
    assert raised.value.written == ()


def test_final_config_pathname_swap_blocks_noop_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    toolchain = root / "lean-toolchain"
    original_metadata = toolchain.stat()
    original_identity = original_metadata.st_dev, original_metadata.st_ino
    replacement = tmp_path / "replacement-toolchain"
    replacement.write_bytes(toolchain.read_bytes())
    original_read = repair_module.os.read
    toolchain_reads = 0
    swapped = False

    def swap_pathname_on_final_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped, toolchain_reads
        content = original_read(descriptor, count)
        metadata = os.fstat(descriptor)
        if content and (metadata.st_dev, metadata.st_ino) == original_identity:
            toolchain_reads += 1
            if toolchain_reads == 3:
                os.replace(replacement, toolchain)
                swapped = True
        return content

    monkeypatch.setattr(repair_module.os, "read", swap_pathname_on_final_read)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert swapped
    assert raised.value.code == "project-repair-race-conflict"
    assert raised.value.written == ()
    assert raised.value.conflicts[0].path == "lean-toolchain"


@pytest.mark.parametrize("error_type", [OSError, FileExistsError])
def test_rename_error_after_commit_reports_uncertain_publication(
    error_type: type[OSError], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module._rename_noreplace

    def commit_then_raise(*args):
        original(*args)
        raise error_type("injected post-commit error")

    monkeypatch.setattr(repair_module, "_rename_noreplace", commit_then_raise)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-commit-uncertain"
    assert raised.value.written == ("mkdocs.yml",)
    assert (root / "mkdocs.yml").is_file()
    assert not list(root.glob(".mkdocs.yml.autoform-repair-*"))
    assert not any(
        conflict.code == "project-repair-recovery-required"
        for conflict in raised.value.conflicts
    )


def test_rename_error_without_commit_reports_retained_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    def fail_without_commit(*args):
        raise OSError("injected pre-commit error")

    monkeypatch.setattr(repair_module, "_rename_noreplace", fail_without_commit)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert not (root / "mkdocs.yml").exists()
    temporary, = root.glob(".mkdocs.yml.autoform-repair-*")
    assert raised.value.conflicts[-1].path == temporary.name


def test_moved_destination_after_commit_is_reported_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()
    original = repair_module._rename_noreplace

    def commit_move_then_raise(source_parent, source, target_parent, target):
        original(source_parent, source, target_parent, target)
        os.rename(
            target,
            "moved-mkdocs.yml",
            src_dir_fd=target_parent,
            dst_dir_fd=target_parent,
        )
        raise OSError("injected post-commit move")

    monkeypatch.setattr(repair_module, "_rename_noreplace", commit_move_then_raise)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-commit-uncertain"
    assert raised.value.written == ("mkdocs.yml",)
    assert not (root / "mkdocs.yml").exists()
    assert (root / "moved-mkdocs.yml").is_file()


def test_final_fstat_failure_after_publish_reports_written_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    destination = root / "mkdocs.yml"
    destination.unlink()
    original_rename = repair_module._rename_noreplace
    original_signature = repair_module._stat_signature
    staged_inode = None
    published = False

    original_write = repair_module.os.write

    def tracked_write(descriptor, content):
        nonlocal staged_inode
        staged_inode = repair_module.os.fstat(descriptor).st_ino
        return original_write(descriptor, content)

    def publish(*args):
        nonlocal published
        original_rename(*args)
        published = True

    def fail_final_signature(metadata):
        if published and metadata.st_ino == staged_inode:
            raise OSError("injected final fstat handling failure")
        return original_signature(metadata)

    monkeypatch.setattr(repair_module.os, "write", tracked_write)
    monkeypatch.setattr(repair_module, "_rename_noreplace", publish)
    monkeypatch.setattr(repair_module, "_stat_signature", fail_final_signature)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ("mkdocs.yml",)
    assert destination.is_file()


def test_dry_run_revalidates_every_planned_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    destination = root / "mkdocs.yml"
    destination.unlink()
    original = repair_module._validate_parent_chain
    inserted = False

    def insert_after_parent_validation(root_descriptor, path):
        nonlocal inserted
        original(root_descriptor, path)
        if not inserted:
            inserted = True
            destination.write_bytes(b"concurrent authored content\n")

    monkeypatch.setattr(
        repair_module, "_validate_parent_chain", insert_after_parent_validation
    )
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root, dry_run=True)

    assert raised.value.code == "project-repair-race-conflict"
    assert raised.value.written == ()
    assert destination.read_bytes() == b"concurrent authored content\n"


def test_other_planned_path_appearance_blocks_first_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    first = root / "blueprint/coverage/README.md"
    second = root / "mkdocs.yml"
    first.unlink()
    second.unlink()
    original = repair_module._publish
    inserted = False

    def insert_before_first_publish(*args, **kwargs):
        nonlocal inserted
        if not inserted:
            inserted = True
            second.write_bytes(b"concurrent authored content\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(repair_module, "_publish", insert_before_first_publish)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert raised.value.written == ()
    assert not first.exists()
    assert second.read_bytes() == b"concurrent authored content\n"
    assert list((root / "blueprint/coverage").glob(".README.md.autoform-repair-*"))


def test_failure_never_attempts_destructive_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "mkdocs.yml").unlink()

    def fail_write(*args, **kwargs):
        raise OSError("injected")

    def forbidden(*args, **kwargs):
        raise AssertionError("repair attempted destructive cleanup")

    monkeypatch.setattr(repair_module.os, "write", fail_write)
    monkeypatch.setattr(repair_module.os, "unlink", forbidden)
    monkeypatch.setattr(repair_module.os, "rmdir", forbidden)
    monkeypatch.setattr(shutil, "rmtree", forbidden)
    with pytest.raises(ProjectRepairError) as raised:
        _repair(root)

    assert raised.value.code == "project-repair-recovery-required"
    assert list(root.glob(".mkdocs.yml.autoform-repair-*"))


def test_partial_workflow_requires_matching_explicit_provenance(
    tmp_path: Path,
) -> None:
    source = "https://github.com/facebookresearch/autoform-bot.git"
    original_ref = "1" * 40
    root = tmp_path / "project"
    create_project(
        root,
        package="Project",
        release_id=_RELEASE,
        autoform_source=source,
        autoform_ref=original_ref,
    )
    missing = root / ".github/workflows/blueprint-pages.yml"
    missing.unlink()

    with pytest.raises(ProjectRepairError) as raised:
        repair_project(
            root,
            autoform_source=source,
            autoform_ref="2" * 40,
        )

    assert raised.value.written == ()
    assert raised.value.conflicts[0].code == "project-repair-workflow-mismatch"
    assert not missing.exists()

    result = repair_project(
        root,
        autoform_source=source,
        autoform_ref=original_ref,
    )
    assert result.written == (".github/workflows/blueprint-pages.yml",)


def test_existing_package_name_is_reported_but_never_used_as_title(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8").replace(
            'name = "Project"', 'name = "lowercase-project"', 1
        ),
        encoding="utf-8",
    )
    missing = root / "blueprint/coverage/README.md"
    missing.unlink()

    result = repair_project(root)

    assert result.package == "lowercase-project"
    assert result.written == ("blueprint/coverage/README.md",)
    assert b"lowercase-project" not in missing.read_bytes()


def test_parameter_map_covers_every_scaffold_placeholder() -> None:
    from autoform_cli import scaffold as scaffold_module

    input_for_placeholder = {
        "PROJECT_TITLE": "title",
        "PROJECT_TITLE_YAML": "title",
        "REPO_URL_YAML": "repository-url",
        "AUTOFORM_SOURCE_YAML": "autoform-source",
        "AUTOFORM_REF_YAML": "autoform-ref",
    }
    found = set()
    for template in scaffold_module._TEMPLATES.rglob("*"):
        if not template.is_file():
            continue
        relative = template.relative_to(scaffold_module._TEMPLATES).as_posix()
        destination = scaffold_module._destination(relative)
        for match in scaffold_module._TEMPLATE_PLACEHOLDER.finditer(
            template.read_text(encoding="utf-8")
        ):
            placeholder = match.group("name")
            found.add(placeholder)
            assert input_for_placeholder[placeholder] in repair_module._REQUIRED_INPUTS[
                destination
            ]

    assert found == set(input_for_placeholder)
