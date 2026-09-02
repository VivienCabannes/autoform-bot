from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_worker.gate_bundle import (
    RUNTIME_BUNDLE_MANIFEST,
    RUNTIME_BUNDLE_SCHEMA,
    LakePackageIdentity,
    RuntimeBundleManifest,
    build_runtime_bundle_manifest,
    load_and_verify_runtime_bundle,
    validate_project_bundle_compatibility,
)
from autoform_worker.gate_provider import GateProviderError


def _packages() -> list[dict[str, object]]:
    return [
        {
            "configFile": "lakefile.toml",
            "inherited": True,
            "inputRev": "main",
            "manifestFile": "lake-manifest.json",
            "name": "batteries",
            "rev": "2" * 40,
            "scope": "leanprover-community",
            "subDir": None,
            "type": "git",
            "url": "https://github.com/leanprover-community/batteries",
        },
        {
            "configFile": "lakefile.lean",
            "inherited": False,
            "inputRev": "v4.32.2",
            "manifestFile": "lake-manifest.json",
            "name": "mathlib",
            "rev": "1" * 40,
            "scope": "",
            "subDir": None,
            "type": "git",
            "url": "https://github.com/leanprover-community/mathlib4.git",
        },
    ]


def _lake_manifest(*, packages: list[dict[str, object]] | None = None) -> bytes:
    return json.dumps(
        {
            "fixedToolchain": False,
            "lakeDir": ".lake",
            "name": "ConsumerProject",
            "packages": _packages() if packages is None else packages,
            "packagesDir": ".lake/packages",
            "version": "1.2.0",
        },
        separators=(",", ":"),
    ).encode()


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    (root / "batteries" / "Batteries").mkdir(parents=True)
    (root / "mathlib" / "Mathlib").mkdir(parents=True)
    (root / "batteries" / "Batteries" / "Basic.lean").write_text("def basic := 1\n")
    (root / "mathlib" / "Mathlib" / "Algebra.lean").write_text("theorem algebra : True := trivial\n")
    (root / "mathlib" / "LICENSE").write_text("license\n")
    (root / "mathlib" / "Mathlib" / "LICENSE.link").symlink_to("../LICENSE")
    return root


def _manifest(root: Path, *, lake_manifest: bytes | None = None) -> RuntimeBundleManifest:
    return build_runtime_bundle_manifest(
        root,
        release_id="gate-runtime-2026.09.02",
        platform="linux/arm64",
        autoform_version="0.5.0",
        autoform_revision="a" * 40,
        lean_toolchain="leanprover/lean4:v4.32.2",
        lean_version="Lean (version 4.32.2, aarch64-unknown-linux-gnu, Release)\n",
        lake_version="Lake version 5.0.0-src+abc (Lean version 4.32.2)\n",
        git_version="git version 2.51.0\n",
        python_version="Python 3.13.7\n",
        lake_manifest=_lake_manifest() if lake_manifest is None else lake_manifest,
    )


def _publish(root: Path, manifest: RuntimeBundleManifest) -> None:
    (root / RUNTIME_BUNDLE_MANIFEST).write_bytes(manifest.evidence_bytes())


def _project(tmp_path: Path, *, lake_manifest: bytes | None = None) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    (root / "lake-manifest.json").write_bytes(_lake_manifest() if lake_manifest is None else lake_manifest)
    return root


def test_runtime_bundle_round_trips_canonical_manifest_and_tree(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    manifest = _manifest(root)
    _publish(root, manifest)

    loaded = load_and_verify_runtime_bundle(root)

    assert loaded == manifest
    assert loaded.schema == RUNTIME_BUNDLE_SCHEMA
    assert loaded.tree.entry_count == 8
    assert loaded.tree.directory_count == 4
    assert loaded.tree.file_count == 3
    assert loaded.tree.symlink_count == 1
    assert loaded.tree.file_bytes == sum(
        len(value.encode()) for value in ("def basic := 1\n", "theorem algebra : True := trivial\n", "license\n")
    )
    assert loaded.evidence_bytes() == (root / RUNTIME_BUNDLE_MANIFEST).read_bytes()
    assert loaded.evidence_bytes().endswith(b"}")
    assert not loaded.evidence_bytes().endswith(b"\n")
    assert [package.name for package in loaded.lake_lock.packages] == ["batteries", "mathlib"]


@pytest.mark.parametrize(
    "invalid_path",
    [
        pytest.param("\ud800", id="unicode"),
        pytest.param("embedded\0nul", id="nul"),
        pytest.param(object(), id="type"),
    ],
)
@pytest.mark.parametrize("entry_point", ["build", "load", "compatibility"])
def test_runtime_bundle_public_entry_points_wrap_invalid_paths(
    tmp_path: Path,
    invalid_path: object,
    entry_point: str,
) -> None:
    root = _bundle(tmp_path)
    manifest = _manifest(root)

    with pytest.raises(GateProviderError, match="cannot be inspected"):
        if entry_point == "build":
            build_runtime_bundle_manifest(
                invalid_path,  # type: ignore[arg-type]
                release_id="gate-runtime-2026.09.02",
                platform="linux/arm64",
                autoform_version="0.5.0",
                autoform_revision="a" * 40,
                lean_toolchain="leanprover/lean4:v4.32.2",
                lean_version="Lean (version 4.32.2, aarch64-unknown-linux-gnu, Release)\n",
                lake_version="Lake version 5.0.0-src+abc (Lean version 4.32.2)\n",
                git_version="git version 2.51.0\n",
                python_version="Python 3.13.7\n",
                lake_manifest=_lake_manifest(),
            )
        elif entry_point == "load":
            load_and_verify_runtime_bundle(invalid_path)  # type: ignore[arg-type]
        else:
            validate_project_bundle_compatibility(
                manifest,
                invalid_path,  # type: ignore[arg-type]
                tracked_source_paths=("lake-manifest.json", "lean-toolchain"),
            )


def test_tree_identity_is_deterministic_and_excludes_only_root_manifest(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    first = _manifest(root)
    _publish(root, first)
    second = _manifest(root)
    assert second.tree == first.tree

    nested = root / "mathlib" / RUNTIME_BUNDLE_MANIFEST
    nested.write_text("nested evidence\n")
    third = _manifest(root)

    assert third.tree != first.tree
    assert third.tree.file_count == first.tree.file_count + 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: b" " + value,
        lambda value: value + b"\n",
        lambda value: value.replace(
            b'"schema":"autoform-gate-runtime-bundle/v1"',
            b'"schema":"autoform-gate-runtime-bundle/v1","schema":"autoform-gate-runtime-bundle/v1"',
        ),
        lambda value: value.replace(b'"tools":{', b'"tools":{"extra":"x",'),
    ],
)
def test_manifest_loader_rejects_noncanonical_or_nonexact_json(
    tmp_path: Path,
    mutation: object,
) -> None:
    root = _bundle(tmp_path)
    manifest = _manifest(root)
    assert callable(mutation)
    (root / RUNTIME_BUNDLE_MANIFEST).write_bytes(mutation(manifest.evidence_bytes()))

    with pytest.raises(GateProviderError):
        load_and_verify_runtime_bundle(root)


def test_manifest_loader_rejects_wrong_scalar_type(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    value = _manifest(root).as_dict()
    value["tree"]["file_count"] = False  # type: ignore[index]
    (root / RUNTIME_BUNDLE_MANIFEST).write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

    with pytest.raises(GateProviderError, match="nonnegative integer"):
        load_and_verify_runtime_bundle(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "darwin/arm64"),
        ("autoform_revision", "a" * 12),
        ("lean_version", "Lean 4.32.2"),
        ("release_id", "release id"),
    ],
)
def test_builder_rejects_ambiguous_release_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root = _bundle(tmp_path)
    arguments = {
        "release_id": "release-1",
        "platform": "linux/amd64",
        "autoform_version": "0.5.0",
        "autoform_revision": "a" * 40,
        "lean_toolchain": "leanprover/lean4:v4.32.2",
        "lean_version": "Lean 4.32.2\n",
        "lake_version": "Lake 5.0.0\n",
        "git_version": "git version 2.51.0\n",
        "python_version": "Python 3.13.7\n",
        "lake_manifest": _lake_manifest(),
    }
    arguments[field] = value

    with pytest.raises(GateProviderError):
        build_runtime_bundle_manifest(root, **arguments)


def test_verifier_rejects_changed_missing_and_extra_tree_entries(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    manifest = _manifest(root)
    _publish(root, manifest)
    changed = root / "mathlib" / "LICENSE"
    changed.write_text("different\n")

    with pytest.raises(GateProviderError, match="does not match"):
        load_and_verify_runtime_bundle(root)

    changed.write_text("license\n")
    (root / "extra").write_text("not locked\n")
    with pytest.raises(GateProviderError, match="outside its dependency lock"):
        load_and_verify_runtime_bundle(root)


@pytest.mark.parametrize("entry_type", ["fifo", "socket"])
def test_tree_rejects_special_entries(tmp_path: Path, entry_type: str) -> None:
    root = _bundle(tmp_path)
    special = root / "mathlib" / entry_type
    listener: socket.socket | None = None
    if entry_type == "fifo":
        os.mkfifo(special)
    else:
        short_directory = Path(tempfile.mkdtemp(prefix="afb-", dir="/tmp"))
        short_socket = short_directory / "s"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(os.fspath(short_socket))
        os.rename(short_socket, special)
    try:
        with pytest.raises(GateProviderError, match="device, socket, FIFO"):
            _manifest(root)
    finally:
        if listener is not None:
            listener.close()
            shutil.rmtree(short_directory)


def test_tree_rejects_control_path_text(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "mathlib" / "bad\nname").write_text("bad\n")

    with pytest.raises(GateProviderError, match="control text"):
        _manifest(root)


def test_tree_rejects_hard_link_aliases(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    os.link(root / "mathlib" / "LICENSE", root / "mathlib" / "LICENSE.alias")

    with pytest.raises(GateProviderError, match="hard-linked"):
        _manifest(root)


@pytest.mark.parametrize("target", ["../../batteries/Batteries/Basic.lean", "/etc/passwd"])
def test_tree_rejects_symlinks_outside_their_individual_package(
    tmp_path: Path,
    target: str,
) -> None:
    root = _bundle(tmp_path)
    (root / "mathlib" / "escape").symlink_to(target)

    with pytest.raises(GateProviderError, match="symbolic link"):
        _manifest(root)


def test_tree_rejects_broken_and_cyclic_symlinks(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    broken = root / "mathlib" / "broken"
    broken.symlink_to("missing")
    with pytest.raises(GateProviderError, match="target"):
        _manifest(root)

    broken.unlink()
    (root / "mathlib" / "first").symlink_to("second")
    (root / "mathlib" / "second").symlink_to("first")
    with pytest.raises(GateProviderError, match="cycle"):
        _manifest(root)


def test_tree_hash_binds_symlink_text_path_and_type(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    alias = root / "mathlib" / "alias"
    alias.symlink_to("LICENSE")
    link = root / "mathlib" / "Mathlib" / "LICENSE.link"
    first = _manifest(root).tree.sha256

    link.unlink()
    link.symlink_to("../alias")
    second = _manifest(root).tree.sha256
    link.rename(root / "mathlib" / "Mathlib" / "LICENSE.renamed")
    third = _manifest(root).tree.sha256
    alias.unlink()
    alias.write_text("license\n")
    fourth = _manifest(root).tree.sha256

    assert len({first, second, third, fourth}) == 4


def test_tree_limits_are_enforced_before_oversized_file_is_hashed(tmp_path: Path) -> None:
    root = _bundle(tmp_path)

    with pytest.raises(GateProviderError, match="byte limit"):
        _manifest_with_limits(root, maximum_file_bytes=1)
    with pytest.raises(GateProviderError, match="entry limit"):
        _manifest_with_limits(root, maximum_entries=1)


def _manifest_with_limits(root: Path, **limits: int) -> RuntimeBundleManifest:
    return build_runtime_bundle_manifest(
        root,
        release_id="release-1",
        platform="linux/amd64",
        autoform_version="0.5.0",
        autoform_revision="a" * 40,
        lean_toolchain="leanprover/lean4:v4.32.2",
        lean_version="Lean 4.32.2\n",
        lake_version="Lake 5.0.0\n",
        git_version="git version 2.51.0\n",
        python_version="Python 3.13.7\n",
        lake_manifest=_lake_manifest(),
        **limits,
    )


def test_project_compatibility_accepts_exact_semantic_lock_projection(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    reordered = _lake_manifest(packages=list(reversed(_packages())))
    project = _project(tmp_path, lake_manifest=reordered)

    validate_project_bundle_compatibility(
        manifest,
        project,
        tracked_source_paths=("lake-manifest.json", "lean-toolchain", "Main.lean"),
    )


def test_project_compatibility_rejects_any_dependency_identity_change(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    changed = _packages()
    changed[1]["inputRev"] = "v4.32.3"
    project = _project(tmp_path, lake_manifest=_lake_manifest(packages=changed))

    with pytest.raises(GateProviderError, match="dependency identities"):
        validate_project_bundle_compatibility(
            manifest,
            project,
            tracked_source_paths=("lake-manifest.json", "lean-toolchain"),
        )


def test_project_compatibility_rejects_toolchain_drift(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    project = _project(tmp_path)
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n")

    with pytest.raises(GateProviderError, match="lean-toolchain"):
        validate_project_bundle_compatibility(
            manifest,
            project,
            tracked_source_paths=("lake-manifest.json", "lean-toolchain"),
        )


@pytest.mark.parametrize(
    "tracked",
    [(".lake/packages/mathlib",), (".Lake/cache",), ("nested/.lake/cache",)],
)
def test_project_compatibility_rejects_tracked_lake_tree(
    tmp_path: Path,
    tracked: tuple[str, ...],
) -> None:
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    project = _project(tmp_path)

    with pytest.raises(GateProviderError, match="must not track .lake"):
        validate_project_bundle_compatibility(
            manifest,
            project,
            tracked_source_paths=("lake-manifest.json", "lean-toolchain", *tracked),
        )


@pytest.mark.parametrize(
    "tracked",
    [
        ("lean-toolchain",),
        ("lake-manifest.json", "lean-toolchain", "../outside"),
        ("lake-manifest.json", "lean-toolchain", "bad\npath"),
        ("lake-manifest.json", "lean-toolchain", "lean-toolchain"),
    ],
)
def test_project_compatibility_rejects_incomplete_or_invalid_tracked_sets(
    tmp_path: Path,
    tracked: tuple[str, ...],
) -> None:
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    project = _project(tmp_path)

    with pytest.raises(GateProviderError):
        validate_project_bundle_compatibility(
            manifest,
            project,
            tracked_source_paths=tracked,
        )


def test_project_compatibility_rejects_symlinked_identity_files(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    project = _project(tmp_path)
    target = project / "toolchain-target"
    target.write_text("leanprover/lean4:v4.32.2\n")
    (project / "lean-toolchain").unlink()
    (project / "lean-toolchain").symlink_to(target.name)

    with pytest.raises(GateProviderError, match="regular file"):
        validate_project_bundle_compatibility(
            manifest,
            project,
            tracked_source_paths=("lake-manifest.json", "lean-toolchain", "toolchain-target"),
        )


def test_lake_lock_rejects_unpinned_git_dependency_and_duplicate_names(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    unpinned = _packages()
    unpinned[0]["rev"] = "main"
    with pytest.raises(GateProviderError, match="full revision"):
        _manifest(root, lake_manifest=_lake_manifest(packages=unpinned))

    duplicate = _packages()
    duplicate[0]["name"] = "mathlib"
    with pytest.raises(GateProviderError, match="unique"):
        _manifest(root, lake_manifest=_lake_manifest(packages=duplicate))

    direct = _packages()[0]
    direct["name"] = "direct"
    direct["rev"] = "main"
    with pytest.raises(GateProviderError, match="full revision"):
        LakePackageIdentity(
            name="direct",
            canonical_json=json.dumps(direct, sort_keys=True, separators=(",", ":")).encode(),
        )


@pytest.mark.parametrize("package_type", ["path", "registry", "git+ssh"])
def test_lake_lock_rejects_non_git_dependency_sources(
    tmp_path: Path,
    package_type: str,
) -> None:
    root = _bundle(tmp_path)
    packages = _packages()
    packages[0]["type"] = package_type

    with pytest.raises(GateProviderError, match="only immutable Git"):
        _manifest(root, lake_manifest=_lake_manifest(packages=packages))


@pytest.mark.parametrize("field", ["configFile", "manifestFile", "subDir"])
@pytest.mark.parametrize("value", ["../outside", "/outside", "nested/../../outside"])
def test_lake_lock_rejects_dependency_paths_outside_package_root(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root = _bundle(tmp_path)
    packages = _packages()
    packages[0][field] = value

    with pytest.raises(GateProviderError, match=field):
        _manifest(root, lake_manifest=_lake_manifest(packages=packages))


def test_lake_lock_requires_a_nonnull_relative_config_file(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    packages = _packages()
    packages[0]["configFile"] = None

    with pytest.raises(GateProviderError, match="configFile"):
        _manifest(root, lake_manifest=_lake_manifest(packages=packages))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(version="1.3.0"), "version"),
        (lambda value: value.update(lakeDir="../lake"), "lakeDir"),
        (lambda value: value.update(fixedToolchain="false"), "fixedToolchain"),
        (lambda value: value.update(extra="future"), "fields"),
    ],
)
def test_lake_lock_rejects_unsupported_manifest_semantics(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    root = _bundle(tmp_path)
    value = json.loads(_lake_manifest())
    assert callable(mutation)
    mutation(value)

    with pytest.raises(GateProviderError, match=message):
        _manifest(root, lake_manifest=json.dumps(value).encode())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda package: package.pop("inherited"),
        lambda package: package.update(inherited="true"),
        lambda package: package.update(extra="future"),
    ],
)
def test_lake_lock_rejects_incomplete_or_extended_package_schema(
    tmp_path: Path,
    mutation: object,
) -> None:
    root = _bundle(tmp_path)
    packages = _packages()
    assert callable(mutation)
    mutation(packages[0])

    with pytest.raises(GateProviderError):
        _manifest(root, lake_manifest=_lake_manifest(packages=packages))


def test_runtime_bundle_rejects_floating_lean_toolchain(tmp_path: Path) -> None:
    root = _bundle(tmp_path)

    with pytest.raises(GateProviderError, match="pinned"):
        build_runtime_bundle_manifest(
            root,
            release_id="release-1",
            platform="linux/amd64",
            autoform_version="0.5.0",
            autoform_revision="a" * 40,
            lean_toolchain="leanprover/lean4:stable",
            lean_version="Lean 4.32.2\n",
            lake_version="Lake 5.0.0\n",
            git_version="git version 2.51.0\n",
            python_version="Python 3.13.7\n",
            lake_manifest=_lake_manifest(),
        )


def test_manifest_object_rejects_internally_inconsistent_tree_counts(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    manifest = _manifest(root)

    with pytest.raises(GateProviderError, match="do not add up"):
        replace(manifest.tree, entry_count=manifest.tree.entry_count + 1)
