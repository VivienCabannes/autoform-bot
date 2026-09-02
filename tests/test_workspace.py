from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.doctor import diagnose_project
from autoform_cli.runtime import RuntimeProjectionError, resolve_runtime_paths
from autoform_cli.visualize import main as visualize_main
from autoform_cli import scaffold as scaffold_module
from autoform_cli import workspace as workspace_reader_module
from autoform_cli import workspace_mutation as workspace_module
from autoform_cli.workspace import (
    discover_workspace,
    inspect_workspace,
    load_workspace,
)
from autoform_cli.workspace_manifest import (
    BLUEPRINT_CHANGE_SCHEMA,
    BLUEPRINT_LIST_SCHEMA,
    MAX_MANIFEST_BYTES,
    WORKSPACE_ERROR_SCHEMA,
    WORKSPACE_INIT_SCHEMA,
    WORKSPACE_SCHEMA,
    WorkspaceError,
    parse_workspace,
)
from autoform_cli.workspace_mutation import (
    create_blueprint_project,
    initialize_workspace,
    register_blueprint_project,
)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    initialize_workspace(root, blueprint_root="Plans", location_id="roadmaps")
    return root


def test_manifest_uses_repository_neutral_named_locations() -> None:
    manifest = parse_workspace(
        'schema = "autoform-workspace/v1"\n\n'
        "[locations.plans]\n"
        'path = "docs/formalization"\n'
        'provides = ["blueprints"]\n\n'
        "[locations.library]\n"
        'path = "LeanProject"\n'
        'provides = ["lean-source"]\n\n'
        '[projects."problem-a"]\n'
        'title = "Problem A"\n'
        'blueprint = { location = "plans", path = "ProblemA" }\n'
    )

    assert manifest.schema == WORKSPACE_SCHEMA
    assert [location.id for location in manifest.locations] == ["library", "plans"]
    assert manifest.project("problem-a").blueprint_location == "plans"
    assert manifest.project("problem-a").blueprint_path == "ProblemA"


@pytest.mark.parametrize(
    ("text", "unknown"),
    [
        (
            'schema = "autoform-workspace/v1"\nproject = {}\n'
            '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n',
            "project",
        ),
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.plans]\npath = "Plans"\nprovide = ["blueprints"]\n',
            "provide",
        ),
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n'
            '[projects.one]\ntitel = "One"\n'
            'blueprint = { location = "plans", path = "One" }\n',
            "titel",
        ),
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n'
            '[projects.one]\nblueprint = { location = "plans", paths = "One" }\n',
            "paths",
        ),
    ],
)
def test_manifest_rejects_unknown_keys_at_every_schema_level(text: str, unknown: str) -> None:
    with pytest.raises(WorkspaceError, match=unknown):
        parse_workspace(text)


@pytest.mark.parametrize(
    "text",
    [
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.plans]\npath = ".git"\nprovides = ["blueprints"]\n'
        ),
        (
            'schema = "autoform-workspace/v1"\n'
            '[locations.root]\npath = "."\nprovides = ["blueprints"]\n'
            '[projects.control]\n'
            'blueprint = { location = "root", path = ".autoform.toml" }\n'
        ),
    ],
)
def test_manifest_rejects_reserved_repository_paths(text: str) -> None:
    with pytest.raises(WorkspaceError, match="reserved repository path"):
        parse_workspace(text)


def test_workspace_init_creates_only_root_manifest_and_collection(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    result = initialize_workspace(root, blueprint_root="Blueprint", location_id="plans")

    assert result.manifest_path == ".autoform.toml"
    assert (root / ".autoform.toml").is_file()
    assert (root / "Blueprint").is_dir()
    assert list((root / "Blueprint").iterdir()) == []
    assert {path.name for path in root.iterdir()} == {".autoform.toml", "Blueprint"}


def test_workspace_manifest_name_must_have_canonical_case(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    alias = root / ".AUTOFORM.TOML"
    alias.write_text(
        'schema = "autoform-workspace/v1"\n'
        '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n'
        '[projects]\n',
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="not portable"):
        initialize_workspace(root, blueprint_root="Plans")
    with pytest.raises(WorkspaceError, match="not portable"):
        load_workspace(root)
    with pytest.raises(WorkspaceError, match="not portable"):
        discover_workspace(root)

    assert {path.name for path in root.iterdir()} == {alias.name}


def test_workspace_init_rejects_case_colliding_collection(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "blueprint").mkdir()

    with pytest.raises(WorkspaceError, match="not portable"):
        initialize_workspace(root, blueprint_root="Blueprint")

    assert not (root / ".autoform.toml").exists()


def test_workspace_init_preserves_existing_unmanaged_collection_contents(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    existing = root / "Blueprint/Hartshorne"
    existing.mkdir(parents=True)
    readme = existing / "README.md"
    readme.write_text("# Existing blueprint\n", encoding="utf-8")

    initialize_workspace(root, blueprint_root="Blueprint")

    assert readme.read_text(encoding="utf-8") == "# Existing blueprint\n"
    assert discover_workspace(root).manifest.projects == ()


def test_multiple_blueprints_are_registered_without_nested_markers(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    unregistered = root / "Plans" / "ExistingWork"
    unregistered.mkdir()
    (unregistered / "README.md").write_text("# Existing\n", encoding="utf-8")

    first = create_blueprint_project(
        root,
        project_id="synthetic-homotopy",
        title="Synthetic Homotopy",
        path="SyntheticHomotopy",
    )
    second = create_blueprint_project(
        root,
        project_id="open-problem",
        title="Open Problem",
        path="OpenProblem",
    )

    assert first.blueprint_path == "Plans/SyntheticHomotopy"
    assert second.blueprint_path == "Plans/OpenProblem"
    assert first.manifest_backup_path.startswith(".autoform.toml.backup-")
    assert second.manifest_backup_path.startswith(".autoform.toml.backup-")
    assert (root / first.manifest_backup_path).is_file()
    assert (root / second.manifest_backup_path).is_file()
    workspace = discover_workspace(root / "Plans" / "SyntheticHomotopy" / "roadmap")
    assert [project.id for project in workspace.manifest.projects] == [
        "open-problem",
        "synthetic-homotopy",
    ]
    assert not (root / "Plans/SyntheticHomotopy/autoform.toml").exists()
    assert not (root / "Plans/OpenProblem/autoform.toml").exists()
    assert (unregistered / "README.md").read_text(encoding="utf-8") == "# Existing\n"


def test_concurrent_blueprint_registrations_preserve_both_projects(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "autoform_cli",
                "blueprint",
                "new",
                project_id,
                "--workspace",
                str(root),
                "--path",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for project_id, path in (("one", "One"), ("two", "Two"))
    ]
    results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]

    assert [result[2] for result in results] == [0, 0], results
    assert {project.id for project in load_workspace(root).manifest.projects} == {"one", "two"}


def test_registering_project_preserves_manifest_comments(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    manifest = root / ".autoform.toml"
    original = manifest.read_text(encoding="utf-8")
    manifest.write_text(original + "\n# Maintained by this repository.\n", encoding="utf-8")

    create_blueprint_project(root, project_id="example", title="Example", path="Example")

    updated = manifest.read_text(encoding="utf-8")
    assert "# Maintained by this repository." in updated
    assert '[projects."example"]' in updated
    assert 'blueprint = {location = "roadmaps", path = "Example"}' in updated


def test_registering_project_updates_an_inline_projects_table(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    (root / "Plans").mkdir(parents=True)
    manifest = root / ".autoform.toml"
    manifest.write_text(
        '# Repository-owned comment.\n'
        'schema = "autoform-workspace/v1"\n'
        'projects = {} # Keep this representation.\n\n'
        '[locations.roadmaps]\n'
        'path = "Plans"\n'
        'provides = ["blueprints"]\n',
        encoding="utf-8",
    )

    result = create_blueprint_project(
        root,
        project_id="example",
        title="Example",
        path="Example",
    )

    assert result.blueprint_path == "Plans/Example"
    assert load_workspace(root).manifest.project("example").title == "Example"
    updated = manifest.read_text(encoding="utf-8")
    assert "# Repository-owned comment." in updated
    assert "# Keep this representation." in updated
    assert "projects = {" in updated


def test_existing_blueprint_can_be_registered_without_modification(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    (existing / "roadmap/README.md").write_text("# Existing roadmap\n", encoding="utf-8")
    before = (existing / "roadmap/README.md").read_bytes()

    result = register_blueprint_project(
        root,
        project_id="existing",
        title="Existing",
        path="Existing",
    )

    assert result.written == ()
    assert result.manifest_backup_path.startswith(".autoform.toml.backup-")
    assert (existing / "roadmap/README.md").read_bytes() == before
    assert discover_workspace(root).manifest.project("existing").blueprint_path == "Existing"


def test_failed_creation_leaves_recovery_directory_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)

    def interrupted(target: Path, *, title: str):
        (target / "authored.md").write_text(title, encoding="utf-8")
        raise workspace_module.ScaffoldError(["injected failure"])

    monkeypatch.setattr(workspace_module, "scaffold_blueprint", interrupted)

    with pytest.raises(WorkspaceError, match="inspect the unregistered directory"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert (root / "Plans/Example/authored.md").read_text(encoding="utf-8") == "Example"
    assert discover_workspace(root).manifest.projects == ()


def test_creation_keeps_its_destination_binding_while_scaffolding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original_write = scaffold_module._exclusive_write_at
    replaced = False

    def replace_destination_before_write(*args, **kwargs) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            (root / "Plans/Example").rename(root / "held-example")
            (root / "Plans/Example").mkdir()
        original_write(*args, **kwargs)

    monkeypatch.setattr(scaffold_module, "_exclusive_write_at", replace_destination_before_write)

    with pytest.raises(WorkspaceError, match="destination changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert replaced
    assert list((root / "Plans/Example").iterdir()) == []
    assert (root / "held-example/roadmap").is_dir()
    assert load_workspace(root).manifest.projects == ()


def test_creation_refuses_to_register_if_the_location_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original = workspace_module.scaffold_blueprint

    def move_location(target: Path, *, title: str):
        written = original(target, title=title)
        manifest = root / ".autoform.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace('path = "Plans"', 'path = "Moved"'),
            encoding="utf-8",
        )
        return written

    monkeypatch.setattr(workspace_module, "scaffold_blueprint", move_location)

    with pytest.raises(WorkspaceError, match="location changed"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert (root / "Plans/Example").is_dir()
    assert load_workspace(root).manifest.projects == ()


def test_workspace_mutation_fails_before_writing_on_an_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setattr(workspace_module, "fcntl", None)

    with pytest.raises(WorkspaceError, match="platform"):
        create_blueprint_project(root, project_id="example", title="Example", path="Example")

    assert not (root / "Plans/Example").exists()
    assert load_workspace(root).manifest.projects == ()


def test_workspace_init_fails_before_writing_on_an_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(workspace_module, "fcntl", None)

    with pytest.raises(WorkspaceError, match="platform"):
        initialize_workspace(root, blueprint_root="Plans")

    assert list(root.iterdir()) == []


@pytest.mark.parametrize("blueprint_root", [".autoform.toml/vaults", ".git/autoform", ".HG/plans"])
def test_workspace_init_rejects_reserved_roots_without_writing(
    tmp_path: Path, blueprint_root: str
) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    with pytest.raises(WorkspaceError, match="reserved"):
        initialize_workspace(root, blueprint_root=blueprint_root)

    assert list(root.iterdir()) == []


def test_workspace_init_path_collision_restores_exact_tree(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    collision = root / "Control"
    collision.write_text("repository-owned\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="blueprint root"):
        initialize_workspace(root, blueprint_root="Control/Plans")

    assert {path.name for path in root.iterdir()} == {"Control"}
    assert collision.read_text(encoding="utf-8") == "repository-owned\n"


def test_workspace_init_treats_complete_manifest_publication_as_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    manifest = root / ".autoform.toml"
    raced = False
    original_unlink = Path.unlink

    def replace_before_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal raced
        if path == manifest:
            raced = True
            path.rename(root / ".autoform-owned")
            path.write_text("concurrent replacement\n", encoding="utf-8")
        original_unlink(path, *args, **kwargs)

    def fail_fsync(_path: Path) -> None:
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(Path, "unlink", replace_before_unlink)
    monkeypatch.setattr(workspace_module, "_fsync_directory", fail_fsync)
    initialize_workspace(root, blueprint_root="Plans")

    assert not raced
    assert load_workspace(root).manifest.locations[0].path == "Plans"
    assert (root / "Plans").is_dir()


def test_workspace_init_reports_directory_sync_failure_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(workspace_module, "_fsync_directory_descriptor", fail_fsync)

    with pytest.raises(WorkspaceError, match="published .autoform.toml"):
        initialize_workspace(root, blueprint_root="Plans")

    assert load_workspace(root).manifest.locations[0].path == "Plans"
    assert (root / "Plans").is_dir()


def test_workspace_init_retains_paths_instead_of_racing_directory_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    nested = root / "Plans/Nested"
    raced = False
    workspace_module._require_workspace_mutation_support()
    original_open = workspace_module.os.open
    original_rmdir = Path.rmdir
    opened_components: list[int] = []

    def fail_after_creating_nested(path, *args, **kwargs):
        if path == "Nested":
            raise OSError("injected open failure")
        descriptor = original_open(path, *args, **kwargs)
        if path == "Plans":
            opened_components.append(descriptor)
        return descriptor

    def replace_before_rmdir(path: Path) -> None:
        nonlocal raced
        if path == nested:
            raced = True
            path.rename(root / "retained-owned-directory")
            path.mkdir()
        original_rmdir(path)

    monkeypatch.setattr(workspace_module, "_require_workspace_mutation_support", lambda: None)
    monkeypatch.setattr(workspace_module.os, "open", fail_after_creating_nested)
    monkeypatch.setattr(Path, "rmdir", replace_before_rmdir)

    with pytest.raises(WorkspaceError, match="retained complete staged manifest"):
        initialize_workspace(root, blueprint_root="Plans/Nested")

    assert not raced
    assert opened_components
    with pytest.raises(OSError):
        workspace_module.os.fstat(opened_components[-1])
    assert not (root / ".autoform.toml").exists()
    assert nested.is_dir()
    staged, = root.glob("..autoform.toml.*.tmp")
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert parse_workspace(staged.read_text(encoding="utf-8")).locations[0].path == "Plans/Nested"


def test_workspace_init_checks_atomic_publication_support_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(workspace_module, "_atomic_noreplace_available", lambda: False)

    with pytest.raises(WorkspaceError, match="platform"):
        initialize_workspace(root, blueprint_root="Plans")

    assert list(root.iterdir()) == []


def test_workspace_init_does_not_claim_incomplete_stage_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    def fail_stage_fsync(_descriptor: int) -> None:
        raise OSError("injected stage sync failure")

    monkeypatch.setattr(workspace_module.os, "fsync", fail_stage_fsync)

    with pytest.raises(WorkspaceError) as raised:
        initialize_workspace(root, blueprint_root="Plans")

    assert "complete" not in str(raised.value)
    staged, = root.glob("..autoform.toml.*.tmp")
    assert staged.name in str(raised.value)
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert not (root / ".autoform.toml").exists()
    assert not (root / "Plans").exists()


@pytest.mark.parametrize("replacement", [False, True], ids=["absent", "replaced"])
def test_workspace_init_rejects_changed_final_manifest_after_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bool,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace
    displaced = root / "published-manifest"

    def change_after_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        original(source_parent, source, target_parent, target)
        manifest = root / target
        manifest.rename(displaced)
        if replacement:
            manifest.write_text("concurrent replacement\n", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "_rename_noreplace", change_after_publish)

    with pytest.raises(WorkspaceError, match="changed before initialization could continue"):
        initialize_workspace(root, blueprint_root="Plans")

    assert parse_workspace(displaced.read_text(encoding="utf-8")).locations[0].path == "Plans"
    assert (root / "Plans").is_dir()
    manifest = root / ".autoform.toml"
    if replacement:
        assert manifest.read_text(encoding="utf-8") == "concurrent replacement\n"
    else:
        assert not manifest.exists()


def test_workspace_init_rejects_same_inode_stage_mutation_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace

    def mutate_after_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        original(source_parent, source, target_parent, target)
        descriptor = workspace_module.os.open(target, workspace_module.os.O_RDWR, dir_fd=target_parent)
        try:
            workspace_module.os.pwrite(descriptor, b"X", 0)
            workspace_module.os.fsync(descriptor)
        finally:
            workspace_module.os.close(descriptor)

    monkeypatch.setattr(workspace_module, "_rename_noreplace", mutate_after_publish)

    with pytest.raises(WorkspaceError, match="published .autoform.toml changed"):
        initialize_workspace(root, blueprint_root="Plans")

    assert (root / ".autoform.toml").read_bytes().startswith(b"X")


def test_workspace_init_rejects_same_inode_mode_mutation_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace

    def chmod_after_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        original(source_parent, source, target_parent, target)
        workspace_module.os.chmod(target, 0o777, dir_fd=target_parent)

    monkeypatch.setattr(workspace_module, "_rename_noreplace", chmod_after_publish)

    with pytest.raises(WorkspaceError, match="published .autoform.toml changed"):
        initialize_workspace(root, blueprint_root="Plans")

    assert stat.S_IMODE((root / ".autoform.toml").stat().st_mode) == 0o777


def test_workspace_init_rejects_bound_directory_replaced_by_file_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace

    def replace_directory_after_publish(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        original(source_parent, source, target_parent, target)
        (root / "Plans").rename(root / "held-plans")
        (root / "Plans").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "_rename_noreplace", replace_directory_after_publish)

    with pytest.raises(WorkspaceError, match="blueprint root changed"):
        initialize_workspace(root, blueprint_root="Plans")

    assert (root / "Plans").read_text(encoding="utf-8") == "foreign\n"
    assert (root / "held-plans").is_dir()


def test_workspace_init_rejects_replaced_directory_before_descending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    workspace_module._require_workspace_mutation_support()
    original_open = workspace_module.os.open
    replaced = False

    def replace_after_mkdir(path, *args, **kwargs):
        nonlocal replaced
        if path == "Plans" and not replaced:
            replaced = True
            (root / "Plans").rename(root / "created-plans")
            (root / "Plans").mkdir()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "_require_workspace_mutation_support", lambda: None)
    monkeypatch.setattr(workspace_module.os, "open", replace_after_mkdir)

    with pytest.raises(WorkspaceError, match="changed while it was being opened"):
        initialize_workspace(root, blueprint_root="Plans/Nested")

    assert replaced
    assert not (root / ".autoform.toml").exists()
    assert list((root / "Plans").iterdir()) == []
    assert list((root / "created-plans").iterdir()) == []
    staged, = root.glob("..autoform.toml.*.tmp")
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert parse_workspace(staged.read_text(encoding="utf-8")).locations[0].path == "Plans/Nested"


def test_registry_exchange_rejects_an_unbound_concurrent_editor_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    manifest = root / ".autoform.toml"
    original_manifest = manifest.read_bytes()
    displaced = root / "manifest-before-editor"
    foreign = b"concurrent editor replacement\n"
    original_exchange = workspace_module._rename_exchange
    raced = False

    def replace_before_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal raced
        if not raced:
            raced = True
            manifest.rename(displaced)
            manifest.write_bytes(foreign)
        original_exchange(source_parent, source, target_parent, target)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_before_exchange)

    with pytest.raises(WorkspaceError, match="rolled back safely"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert raced
    assert manifest.read_bytes() != foreign
    assert displaced.read_bytes() == original_manifest
    retained, = root.glob("..autoform.toml.*.tmp")
    assert retained.read_bytes() == foreign


def test_registry_exchange_retains_displaced_inode_if_an_editor_mutates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    editor_bytes = b"editor wrote through its old open descriptor\n"

    def mutate_displaced(name: str) -> None:
        if name != "registry-backup-published":
            return
        recovery, = root.glob(".autoform.toml.backup-*")
        with recovery.open("r+b", buffering=0) as stream:
            stream.seek(0)
            stream.write(editor_bytes)
            stream.truncate()
            workspace_module.os.fsync(stream.fileno())

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", mutate_displaced)

    register_blueprint_project(
        root,
        project_id="existing",
        title="Existing",
        path="Existing",
    )

    assert load_workspace(root).manifest.project("existing").blueprint_path == "Existing"
    recovery, = root.glob(".autoform.toml.backup-*")
    assert recovery.read_bytes() == editor_bytes


def test_registry_recovers_success_after_backup_publication_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    original_manifest = (root / ".autoform.toml").read_bytes()

    def fail_after_backup(name: str) -> None:
        if name == "registry-backup-published":
            raise WorkspaceError(["injected after backup publication"])

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", fail_after_backup)

    result = register_blueprint_project(
        root,
        project_id="existing",
        title="Existing",
        path="Existing",
    )

    assert load_workspace(root).manifest.project("existing").blueprint_path == "Existing"
    assert result.manifest_backup_path.startswith(".autoform.toml.backup-")
    assert (root / result.manifest_backup_path).read_bytes() == original_manifest


def test_registry_recovery_does_not_report_a_foreign_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    manifest = root / ".autoform.toml"
    original_manifest = manifest.read_bytes()
    foreign = root / ".autoform.toml.backup-0000"
    foreign.hardlink_to(manifest)

    def fail_after_backup(name: str) -> None:
        if name == "registry-backup-published":
            raise WorkspaceError(["injected after backup publication"])

    monkeypatch.setattr(workspace_module, "_workspace_mutation_checkpoint", fail_after_backup)

    result = register_blueprint_project(
        root,
        project_id="existing",
        title="Existing",
        path="Existing",
    )

    assert result.manifest_backup_path != foreign.name
    assert (root / result.manifest_backup_path).read_bytes() == original_manifest
    assert foreign.read_bytes() == original_manifest


def test_registry_rollback_rejects_a_replaced_displaced_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    existing = root / "Plans/Existing"
    (existing / "roadmap").mkdir(parents=True)
    manifest = root / ".autoform.toml"
    original_manifest = manifest.read_bytes()
    held_prior = root / "held-prior-manifest"
    foreign = b"foreign replacement\n"
    original_exchange = workspace_module._rename_exchange
    exchanged = False

    def replace_displaced_after_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal exchanged
        original_exchange(source_parent, source, target_parent, target)
        if not exchanged:
            exchanged = True
            displaced = root / source
            displaced.rename(held_prior)
            displaced.write_bytes(foreign)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_displaced_after_exchange)

    with pytest.raises(WorkspaceError, match="rolled back safely"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert manifest.read_bytes() != foreign
    assert held_prior.read_bytes() == original_manifest


def test_project_registration_rejects_location_replaced_at_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    original_exchange = workspace_module._rename_exchange
    replaced = False

    def replace_location_before_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            (root / "Plans").rename(root / "held-plans")
            (root / "Plans").write_text("foreign\n", encoding="utf-8")
        original_exchange(source_parent, source, target_parent, target)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_location_before_exchange)

    with pytest.raises(WorkspaceError, match="blueprint location changed"):
        create_blueprint_project(root, project_id="one", title="One", path="One")

    assert replaced
    assert (root / "Plans").read_text(encoding="utf-8") == "foreign\n"
    assert (root / "held-plans/One/roadmap").is_dir()
    assert load_workspace(root).manifest.projects == ()


def test_project_registration_rejects_roadmap_replaced_at_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    (root / "Plans/Existing/roadmap").mkdir(parents=True)
    original_exchange = workspace_module._rename_exchange
    replaced = False

    def replace_roadmap_before_exchange(
        source_parent: int,
        source: str,
        target_parent: int,
        target: str,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            roadmap = root / "Plans/Existing/roadmap"
            roadmap.rename(root / "held-roadmap")
            roadmap.write_text("foreign\n", encoding="utf-8")
        original_exchange(source_parent, source, target_parent, target)

    monkeypatch.setattr(workspace_module, "_rename_exchange", replace_roadmap_before_exchange)

    with pytest.raises(WorkspaceError, match="roadmap changed"):
        register_blueprint_project(
            root,
            project_id="existing",
            title="Existing",
            path="Existing",
        )

    assert replaced
    assert (root / "Plans/Existing/roadmap").read_text(encoding="utf-8") == "foreign\n"
    assert (root / "held-roadmap").is_dir()
    assert load_workspace(root).manifest.projects == ()


def test_concurrent_workspace_init_never_exposes_partial_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    original = workspace_module._rename_noreplace
    original_fchmod = workspace_module.os.fchmod
    ready = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0
    first_stage: list[str] = []
    restrictive_modes: list[int] = []
    errors: list[WorkspaceError] = []

    def observe_final_mode(descriptor: int, mode: int) -> None:
        if mode == 0o644:
            restrictive_modes.append(stat.S_IMODE(workspace_module.os.fstat(descriptor).st_mode))
        original_fchmod(descriptor, mode)

    def pause_first_publish(source_parent: int, source: str, target_parent: int, target: str):
        nonlocal calls
        with lock:
            calls += 1
            first = calls == 1
        if first:
            stage = root / source
            first_stage.append(source)
            assert stat.S_IMODE(stage.stat().st_mode) == 0o644
            parse_workspace(stage.read_text(encoding="utf-8"))
            ready.set()
            assert release.wait(timeout=30)
        return original(source_parent, source, target_parent, target)

    def initialize_first() -> None:
        try:
            initialize_workspace(root, blueprint_root="PlansA")
        except WorkspaceError as error:
            errors.append(error)

    monkeypatch.setattr(workspace_module.os, "fchmod", observe_final_mode)
    monkeypatch.setattr(workspace_module, "_rename_noreplace", pause_first_publish)
    thread = threading.Thread(target=initialize_first)
    thread.start()
    assert ready.wait(timeout=30)
    assert not (root / ".autoform.toml").exists()

    try:
        initialize_workspace(root, blueprint_root="PlansB")
    finally:
        release.set()
        thread.join(timeout=30)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert restrictive_modes and set(restrictive_modes) == {0o600}
    assert first_stage[0] in str(errors[0])
    assert "PlansA" in str(errors[0])
    assert (root / first_stage[0]).is_file()
    assert stat.S_IMODE((root / first_stage[0]).stat().st_mode) == 0o600
    assert load_workspace(root).manifest.locations[0].path == "PlansB"
    assert (root / "PlansB").is_dir()
    assert (root / "PlansA").is_dir()
    assert list((root / "PlansA").iterdir()) == []


def test_concurrent_workspace_init_preserves_winner_and_safe_loser_residue(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "autoform_cli",
                "workspace",
                "init",
                str(root),
                "--blueprint-root",
                collection,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for collection in ("PlansA", "PlansB")
    ]
    results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]

    assert sorted(result[2] for result in results) == [0, 1], results
    manifest = load_workspace(root).manifest
    winner = manifest.locations[0].path
    loser = "PlansB" if winner == "PlansA" else "PlansA"
    assert (root / winner).is_dir()
    if (root / loser).exists():
        assert list((root / loser).iterdir()) == []
    for staged in root.glob("..autoform.toml.*.tmp"):
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
        parse_workspace(staged.read_text(encoding="utf-8"))


def test_workspace_resolution_requires_a_project_at_multi_project_root(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    create_blueprint_project(root, project_id="two", title="Two", path="Two")

    with pytest.raises(RuntimeProjectionError, match="pass --project"):
        resolve_runtime_paths(root)

    selected = resolve_runtime_paths(root, project_id="two")
    assert selected.project_root == root.resolve()
    assert selected.blueprint_dir == (root / "Plans/Two").resolve()
    assert selected.workspace_project_id == "two"
    assert selected.workspace_project_binding_sha256 is not None

    inferred = resolve_runtime_paths(root / "Plans/One/roadmap")
    assert inferred.project_root == root.resolve()
    assert inferred.blueprint_dir == (root / "Plans/One").resolve()
    assert inferred.workspace_project_id == "one"
    assert (
        inferred.workspace_project_binding_sha256
        != selected.workspace_project_binding_sha256
    )


def test_project_selector_requires_a_workspace_manifest(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    (root / "blueprint/roadmap").mkdir(parents=True)

    with pytest.raises(RuntimeProjectionError, match="requires an enclosing"):
        resolve_runtime_paths(root, project_id="example")


def test_single_project_is_not_inferred_from_an_unrelated_directory(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    unrelated = root / "Plans/ExistingWork"
    unrelated.mkdir()

    selected = resolve_runtime_paths(root)
    assert selected.blueprint_dir == (root / "Plans/One").resolve()
    with pytest.raises(RuntimeProjectionError, match="pass --project"):
        resolve_runtime_paths(unrelated)


def test_doctor_resolves_a_named_workspace_project_from_the_repository_root(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    coverage = root / "Plans/One/coverage/README.md"
    coverage.write_text(
        "# Coverage contract\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Empty scaffold | `OUT` | No formalization targets have been selected |\n",
        encoding="utf-8",
    )

    result = diagnose_project(root, project_id="one")

    assert result.clean
    assert result.checks[0].detail == "resolved Plans/One"


def test_workspace_check_visits_only_registered_blueprints(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    create_blueprint_project(root, project_id="two", title="Two", path="Two")
    unregistered = root / "Plans/Unregistered"
    unregistered.mkdir()
    (unregistered / "roadmap").mkdir()
    (unregistered / "roadmap/broken.md").write_text("not a valid article", encoding="utf-8")

    assert main(["workspace", "check", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert [project["project"] for project in payload["projects"]] == ["one", "two"]


def test_workspace_check_refuses_to_succeed_without_registered_projects(
    tmp_path: Path, capsys
) -> None:
    root = _workspace(tmp_path)

    assert main(["workspace", "check", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["projects"] == []
    assert [item["code"] for item in payload["diagnostics"]] == ["projects-empty"]


def test_workspace_check_with_lean_root_rejects_missing_declarations(
    tmp_path: Path, capsys
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    (root / "Plans/One/roadmap/missing.md").write_text(
        "---\ndeclaration: theorem\nlean: Definitely.Missing\n---\n\n# Missing\n",
        encoding="utf-8",
    )

    assert main(["workspace", "check", str(root), "--lean-root", str(root)]) == 1
    assert "declaration not found: Definitely.Missing" in capsys.readouterr().out


def test_workspace_check_rejects_roadmap_symlinks_like_single_project_check(
    tmp_path: Path, capsys
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "Plans/One/roadmap/external").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert main(["workspace", "check", str(root)]) == 1
    assert "roadmap contains a symbolic link" in capsys.readouterr().out


def test_workspace_check_labels_nonfatal_diagnostics_as_warnings(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    manifest = root / ".autoform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[locations.optional]\npath = "Missing"\nprovides = ["lean-source"]\n',
        encoding="utf-8",
    )

    assert main(["workspace", "check", str(root)]) == 0
    captured = capsys.readouterr()
    assert "warning: location-missing Missing" in captured.out
    assert "error: location-missing" not in captured.err


def test_workspace_inspection_reports_a_missing_registered_blueprint(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    for child in sorted((root / "Plans/Example").rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
        else:
            child.rmdir()
    (root / "Plans/Example").rmdir()

    result = inspect_workspace(root)

    assert not result.ok
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["blueprint-missing"]


@pytest.mark.parametrize(
    "path",
    [
        "../Escape",
        "Nested/Blueprint",
        "/absolute",
        "C:/absolute",
        "back\\slash",
        "bad*name",
        "CON",
        "trailing.",
        "trailing ",
        ".",
    ],
)
def test_blueprint_member_must_be_one_portable_child(tmp_path: Path, path: str) -> None:
    root = _workspace(tmp_path)

    with pytest.raises(WorkspaceError, match="immediate child"):
        create_blueprint_project(root, project_id="example", title="Example", path=path)

    assert parse_workspace((root / ".autoform.toml").read_text(encoding="utf-8")).projects == ()


def test_workspace_cli_creates_lists_and_checks_projects(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    assert main(["workspace", "init", str(root), "--blueprint-root", "Roadmaps"]) == 0
    capsys.readouterr()
    assert main(
        [
            "blueprint",
            "new",
            "finite-flat",
            "--workspace",
            str(root),
            "--path",
            "FiniteFlat",
            "--title",
            "Finite Flat",
        ]
    ) == 0
    capsys.readouterr()
    assert main(["blueprint", "list", str(root), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == {
        "ok": True,
        "projects": [
            {"id": "finite-flat", "path": "Roadmaps/FiniteFlat", "title": "Finite Flat"}
        ],
        "schema": BLUEPRINT_LIST_SCHEMA,
    }
    assert main(["check", str(root), "--project", "finite-flat"]) == 0
    assert "OK: 1 articles, 0 dependencies" in capsys.readouterr().out
    assert visualize_main([str(root), "--project", "finite-flat", "--structure"]) == 0
    capsys.readouterr()
    assert (root / "Roadmaps/FiniteFlat/dependencies.md").is_file()
    assert (root / "Roadmaps/FiniteFlat/structure.md").is_file()


def test_workspace_render_defaults_to_project_scoped_output(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="one", title="One", path="One")
    create_blueprint_project(root, project_id="two", title="Two", path="Two")
    monkeypatch.chdir(root)

    assert main(["render", ".", "--project", "one"]) == 0
    capsys.readouterr()
    assert main(["render", ".", "--project", "two"]) == 0
    capsys.readouterr()

    assert (root / "site-src/one/README.md").is_file()
    assert (root / "site-src/two/README.md").is_file()


def test_manifest_rejects_case_colliding_blueprint_paths() -> None:
    with pytest.raises(WorkspaceError, match="same path"):
        parse_workspace(
            'schema = "autoform-workspace/v1"\n'
            "[locations.plans]\n"
            'path = "Blueprint"\n'
            'provides = ["blueprints"]\n'
            "[projects.one]\n"
            'blueprint = { location = "plans", path = "Example" }\n'
            "[projects.two]\n"
            'blueprint = { location = "plans", path = "example" }\n'
        )


def test_manifest_rejects_nested_managed_blueprint_paths() -> None:
    with pytest.raises(WorkspaceError, match="overlaps"):
        parse_workspace(
            'schema = "autoform-workspace/v1"\n'
            "[locations.outer]\n"
            'path = "Blueprint"\n'
            'provides = ["blueprints"]\n'
            "[locations.inner]\n"
            'path = "Blueprint/Outer"\n'
            'provides = ["blueprints"]\n'
            "[projects.outer]\n"
            'blueprint = { location = "outer", path = "Outer" }\n'
            "[projects.inner]\n"
            'blueprint = { location = "inner", path = "Inner" }\n'
        )


def test_manifest_rejects_portably_duplicate_location_paths() -> None:
    with pytest.raises(WorkspaceError, match="same portable path"):
        parse_workspace(
            'schema = "autoform-workspace/v1"\n'
            "[locations.first]\n"
            'path = "Blueprint"\n'
            'provides = ["blueprints"]\n'
            "[locations.second]\n"
            'path = "blueprint"\n'
            'provides = ["blueprints"]\n'
            "[projects]\n"
        )


@pytest.mark.parametrize(
    "path",
    ["C:/Blueprint", "bad*name", "CON", "AUX.txt", "trailing.", " leading", "trailing ", "bad\x7fname"],
)
def test_workspace_paths_are_portable_to_windows(tmp_path: Path, path: str) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    with pytest.raises(WorkspaceError, match="portable"):
        initialize_workspace(root, blueprint_root=path)

    assert not (root / ".autoform.toml").exists()


def test_workspace_project_id_is_safe_as_a_publication_directory(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    with pytest.raises(WorkspaceError, match="project id is not portable"):
        create_blueprint_project(root, project_id="CON", title="Reserved")

    assert list((root / "Plans").iterdir()) == []
    assert load_workspace(root).manifest.projects == ()


def test_workspace_json_results_have_operation_specific_schemas(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    assert main(["workspace", "init", str(root), "--blueprint-root", "Plans", "--json"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["ok"] is True
    assert initialized["schema"] == WORKSPACE_INIT_SCHEMA

    assert main(
        [
            "blueprint",
            "new",
            "example",
            "--workspace",
            str(root),
            "--path",
            "Example",
            "--json",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["ok"] is True
    assert created["schema"] == BLUEPRINT_CHANGE_SCHEMA
    assert created["manifest_backup_path"].startswith(".autoform.toml.backup-")

    assert main(["blueprint", "new", "example", "--workspace", str(root), "--json"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["ok"] is False
    assert failed["schema"] == WORKSPACE_ERROR_SCHEMA


def test_creation_rejects_case_collision_with_unregistered_sibling(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "Plans/ExistingWork").mkdir()

    with pytest.raises(WorkspaceError, match="not portable"):
        create_blueprint_project(
            root,
            project_id="example",
            title="Example",
            path="existingwork",
        )

    assert {path.name for path in (root / "Plans").iterdir()} == {"ExistingWork"}
    assert discover_workspace(root).manifest.projects == ()


def test_loading_rejects_case_colliding_registered_and_unregistered_paths(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_blueprint_project(root, project_id="example", title="Example", path="Example")
    if (root / "Plans/example").exists():
        pytest.skip("case-only sibling names cannot coexist on this filesystem")
    (root / "Plans/example").mkdir()

    with pytest.raises(WorkspaceError, match="not portable"):
        discover_workspace(root)


def test_manifest_rejects_a_symlinked_managed_path(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "Plans").symlink_to(outside, target_is_directory=True)
    (root / ".autoform.toml").write_text(
        'schema = "autoform-workspace/v1"\n'
        "[locations.plans]\n"
        'path = "Plans"\n'
        'provides = ["blueprints"]\n'
        "[projects]\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="symbolic link"):
        discover_workspace(root)


def test_nonregular_manifest_blocks_outer_workspace_discovery(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    initialize_workspace(outer, blueprint_root="Blueprint")
    nested = outer / "nested"
    nested.mkdir()
    (nested / ".autoform.toml").mkdir()

    with pytest.raises(WorkspaceError, match="regular file"):
        discover_workspace(nested)


def test_workspace_manifest_read_is_size_bounded(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    manifest = root / ".autoform.toml"
    with manifest.open("wb") as stream:
        stream.truncate(MAX_MANIFEST_BYTES + 1)

    with pytest.raises(WorkspaceError, match="byte limit"):
        load_workspace(root)


def test_workspace_manifest_read_rejects_same_name_root_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    held_root = tmp_path / "held-repository"
    replacement_manifest = b'repository = "replacement"\n'
    original_read = workspace_reader_module.os.read
    replaced = False

    def replace_root_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        content = original_read(descriptor, size)
        if content and not replaced:
            replaced = True
            root.rename(held_root)
            root.mkdir()
            (root / ".autoform.toml").write_bytes(replacement_manifest)
        return content

    monkeypatch.setattr(workspace_reader_module.os, "read", replace_root_after_read)

    with pytest.raises(WorkspaceError, match="cannot read .autoform.toml safely"):
        load_workspace(root)

    assert replaced
    assert (root / ".autoform.toml").read_bytes() == replacement_manifest
    assert load_workspace(held_root).manifest.locations[0].path == "Plans"
