from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autoform_cli.__main__ import _human_text, main
from autoform_cli.project import (
    PROJECT_INSPECTION_SCHEMA,
    RELEASE_CATALOG_SCHEMA,
    inspect_project,
    load_release_catalog,
    parse_release_catalog,
)
from autoform_cli.project.catalog import ProjectCatalogError


def _project(tmp_path: Path, *, revision: str = "v4.32.2") -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        'version = "0.1.0"\n'
        'defaultTargets = ["Example"]\n\n'
        '[[require]]\nname = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        f'rev = "{revision}"\n\n'
        '[[lean_lib]]\nname = "Example"\nsrcDir = "src"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    _write_mathlib_manifest(
        root,
        input_revision=revision,
        resolved_revision=(
            "905b95818eb32af7874a58b427f50c1711a5e96c"
            if revision == "v4.32.2"
            else "2" * 40
        ),
        scope="",
    )
    return root


def _write_mathlib_manifest(
    root: Path,
    *,
    input_revision: str = "v4.32.2",
    resolved_revision: str = "905b95818eb32af7874a58b427f50c1711a5e96c",
    scope: str = "leanprover-community",
    subdirectory: str | None = None,
    config_file: str = "lakefile.lean",
    manifest_file: str | None = "lake-manifest.json",
    version: str | int = "1.2.0",
) -> None:
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": version,
                "packagesDir": ".lake/packages",
                "packages": [
                    {
                        "url": "https://github.com/leanprover-community/mathlib4",
                        "type": "git",
                        "subDir": subdirectory,
                        "scope": scope,
                        "rev": resolved_revision,
                        "name": "mathlib",
                        "manifestFile": manifest_file,
                        "inputRev": input_revision,
                        "inherited": False,
                        "configFile": config_file,
                    }
                ],
                "name": "Example",
                "lakeDir": ".lake",
                "fixedToolchain": False,
            }
        ),
        encoding="utf-8",
    )


def _write_mathlib_path_override(
    root: Path,
    *,
    directory: str = "vendor/fake-mathlib",
    version: str | int = "1.2.0",
) -> None:
    override = root / ".lake/package-overrides.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps(
            {
                "schemaVersion": version,
                "packages": [
                    {
                        "name": "mathlib",
                        "scope": "",
                        "configFile": "lakefile.toml",
                        "manifestFile": None,
                        "inherited": False,
                        "type": "path",
                        "dir": directory,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_mathlib_git_override(root: Path) -> None:
    manifest = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
    override = root / ".lake/package-overrides.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps(
            {
                "schemaVersion": "1.2.0",
                "packages": [manifest["packages"][0]],
            }
        ),
        encoding="utf-8",
    )


def test_release_catalog_is_canonical() -> None:
    catalog = load_release_catalog()
    assert catalog.schema == RELEASE_CATALOG_SCHEMA
    assert [release.id for release in catalog.releases] == sorted(
        release.id for release in catalog.releases
    )
    assert sum(release.recommended for release in catalog.releases) == 1
    assert catalog.to_json() == catalog.to_json()


def test_recommended_release_matches_the_shipped_example() -> None:
    catalog = load_release_catalog()
    assert catalog.recommended.lean.toolchain == "leanprover/lean4:v4.32.2"
    assert catalog.recommended.mathlib.input_revision == "v4.32.2"
    assert catalog.recommended.mathlib.scope == "leanprover-community"
    assert catalog.recommended.mathlib.name == "mathlib"
    assert catalog.recommended.mathlib.resolved_revision == (
        "905b95818eb32af7874a58b427f50c1711a5e96c"
    )
    assert len(catalog.releases) == 1


def test_release_catalog_v1_serialization_contract() -> None:
    assert load_release_catalog().as_dict() == {
        "schema": "autoform-project-release-catalog/v1",
        "releases": [
            {
                "id": "lean-v4.32.2-mathlib-v4.32.2",
                "channel": "stable",
                "recommended": True,
                "lean": {
                    "toolchain": "leanprover/lean4:v4.32.2",
                    "version": "v4.32.2",
                },
                "mathlib": {
                    "name": "mathlib",
                    "scope": "leanprover-community",
                    "package_type": "git",
                    "git": "https://github.com/leanprover-community/mathlib4",
                    "input_revision": "v4.32.2",
                    "resolved_revision": (
                        "905b95818eb32af7874a58b427f50c1711a5e96c"
                    ),
                    "subdirectory": None,
                    "config_file": "lakefile.lean",
                    "manifest_file": "lake-manifest.json",
                },
            }
        ],
    }


@pytest.mark.parametrize(
    ("git", "resolved_revision"),
    [
        ("file:///tmp/mathlib", "9" * 40),
        ("https://user@example.test/mathlib.git", "9" * 40),
        (" https://github.com/leanprover-community/mathlib4", "9" * 40),
        ("https://github.com/leanprover-community/math\tlib4", "9" * 40),
        ("https://github.com/leanprover-community/mathlib4", "v4.32.2"),
    ],
)
def test_release_catalog_rejects_unverifiable_mathlib_identity(
    git: str, resolved_revision: str
) -> None:
    with pytest.raises(ProjectCatalogError):
        parse_release_catalog(
            {
                "schema": RELEASE_CATALOG_SCHEMA,
                "releases": [
                    {
                        "id": "bad",
                        "channel": "stable",
                        "recommended": True,
                        "lean": {
                            "toolchain": "leanprover/lean4:v4.32.2",
                            "version": "v4.32.2",
                        },
                        "mathlib": {
                            "name": "mathlib",
                            "scope": "leanprover-community",
                            "package_type": "git",
                            "git": git,
                            "input_revision": "v4.32.2",
                            "resolved_revision": resolved_revision,
                            "subdirectory": None,
                            "config_file": "lakefile.lean",
                            "manifest_file": "lake-manifest.json",
                        },
                    }
                ],
            }
        )


def test_recommended_release_matches_a_project_pinned_to_it(tmp_path: Path) -> None:
    recommended = load_release_catalog().recommended
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        f'git = "{recommended.mathlib.git}"\nrev = "{recommended.mathlib.input_revision}"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text(f"{recommended.lean.toolchain}\n", encoding="utf-8")
    _write_mathlib_manifest(root, scope="")

    result = inspect_project(root)
    assert result.ok
    assert result.compatibility.status == "supported"
    assert result.compatibility.release == recommended.id


def test_project_inspection_v1_git_serialization_contract(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path))
    payload = json.loads(result.to_json())

    assert set(payload) == {
        "autoform",
        "compatibility",
        "diagnostics",
        "git_path",
        "lake",
        "lake_manifest_path",
        "lake_manifest_sha256",
        "lean",
        "mathlib",
        "ok",
        "package_overrides_path",
        "package_overrides_sha256",
        "project_root",
        "schema",
    }
    assert payload["schema"] == "autoform-project-inspection/v1"
    assert payload["mathlib"] == {
        "config_file": "lakefile.lean",
        "declared_revision": "v4.32.2",
        "git": "https://github.com/leanprover-community/mathlib4",
        "input_revision": "v4.32.2",
        "manifest_file": "lake-manifest.json",
        "name": "mathlib",
        "package_type": "git",
        "path": None,
        "resolved_revision": "905b95818eb32af7874a58b427f50c1711a5e96c",
        "scope": "",
        "source": "lake-manifest.json",
        "subdirectory": None,
    }


def test_catalog_loader_converts_decode_and_recursion_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli.project import catalog as catalog_module

    class InvalidResource:
        def joinpath(self, _name: str):
            return self

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            raise UnicodeDecodeError("utf-8", b"x", 0, 1, "invalid")

    monkeypatch.setattr(catalog_module, "files", lambda _package: InvalidResource())
    with pytest.raises(ProjectCatalogError):
        load_release_catalog()

    monkeypatch.setattr(catalog_module, "files", lambda _package: type("R", (), {
        "joinpath": lambda self, _name: self,
        "read_text": lambda self, **_kwargs: "{}",
    })())
    monkeypatch.setattr(catalog_module.json, "loads", lambda _text: (_ for _ in ()).throw(RecursionError()))
    with pytest.raises(ProjectCatalogError):
        load_release_catalog()


def test_release_catalog_rejects_invalid_contract() -> None:
    with pytest.raises(ProjectCatalogError):
        parse_release_catalog({"schema": RELEASE_CATALOG_SCHEMA, "releases": []})
    with pytest.raises(ProjectCatalogError):
        parse_release_catalog(
            {
                "schema": RELEASE_CATALOG_SCHEMA,
                "releases": [
                    {
                        "id": "x",
                        "channel": "stable",
                        "recommended": "yes",
                        "lean": {"toolchain": "x", "version": "x"},
                        "mathlib": {"git": "x", "revision": "x"},
                    }
                ],
            }
        )


def test_release_catalog_rejects_a_toolchain_version_mismatch() -> None:
    with pytest.raises(ProjectCatalogError, match="toolchain and version disagree"):
        parse_release_catalog(
            {
                "schema": RELEASE_CATALOG_SCHEMA,
                "releases": [
                    {
                        "id": "contradictory",
                        "channel": "stable",
                        "recommended": True,
                        "lean": {
                            "toolchain": "leanprover/lean4:v4.32.2",
                            "version": "v9.9.9",
                        },
                        "mathlib": {
                            "name": "mathlib",
                            "scope": "leanprover-community",
                            "package_type": "git",
                            "git": "https://github.com/leanprover-community/mathlib4",
                            "input_revision": "v4.32.2",
                            "resolved_revision": "905b95818eb32af7874a58b427f50c1711a5e96c",
                            "subdirectory": None,
                            "config_file": "lakefile.lean",
                            "manifest_file": "lake-manifest.json",
                        },
                    }
                ],
            }
        )


@pytest.mark.parametrize("value", ["line\nbreak", "escape\x1bsequence", "surrogate\ud800"])
def test_release_catalog_rejects_unprintable_strings(value: str) -> None:
    with pytest.raises(ProjectCatalogError):
        parse_release_catalog(
            {
                "schema": RELEASE_CATALOG_SCHEMA,
                "releases": [
                    {
                        "id": value,
                        "channel": "stable",
                        "recommended": True,
                        "lean": {
                            "toolchain": "leanprover/lean4:v4.32.2",
                            "version": "v4.32.2",
                        },
                        "mathlib": {
                            "name": "mathlib",
                            "scope": "leanprover-community",
                            "package_type": "git",
                            "git": "https://github.com/leanprover-community/mathlib4",
                            "input_revision": "v4.32.2",
                            "resolved_revision": "9" * 40,
                            "subdirectory": None,
                            "config_file": "lakefile.lean",
                            "manifest_file": "lake-manifest.json",
                        },
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subdirectory", "."),
        ("subdirectory", "../Mathlib"),
        ("config_file", "./lakefile.lean"),
        ("config_file", "lakefile.lean/"),
        ("manifest_file", "../lake-manifest.json"),
    ],
)
def test_release_catalog_rejects_unmatchable_package_paths(
    field: str, value: str
) -> None:
    payload = json.loads(load_release_catalog().to_json())
    payload["releases"][0]["mathlib"][field] = value

    with pytest.raises(ProjectCatalogError):
        parse_release_catalog(payload)


def test_release_catalog_rejects_duplicate_material_identities() -> None:
    payload = json.loads(load_release_catalog().to_json())
    first = payload["releases"][0]
    first["id"] = "a"
    first["recommended"] = False
    second = json.loads(json.dumps(first))
    second["id"] = "b"
    second["recommended"] = True
    payload["releases"] = [first, second]

    with pytest.raises(ProjectCatalogError, match="duplicate material identities"):
        parse_release_catalog(payload)


def test_inspects_bundled_example_without_host_paths(repo_root: Path) -> None:
    example = repo_root / "skills/setup/assets/cabannes-thesis-project"
    result = inspect_project(example)
    payload = result.as_dict()

    assert result.ok
    assert payload["schema"] == PROJECT_INSPECTION_SCHEMA
    assert payload["project_root"] == "."
    assert payload["lake"]["name"] == "CabannesThesis"
    assert payload["lake"]["targets"] == [
        {
            "kind": "lean_lib",
            "name": "CabannesThesis",
            "root": None,
            "roots": ["CabannesThesis"],
            "src_dir": "src",
        }
    ]
    assert payload["lean"]["version"] == "v4.32.2"
    assert payload["mathlib"] is None
    assert payload["compatibility"]["status"] == "indeterminate"
    assert any(
        diagnostic.code == "missing-lake-manifest"
        for diagnostic in result.diagnostics
    )
    assert payload["autoform"]["detected"] is True
    assert str(repo_root) not in result.to_json()


def test_discovers_nearest_project_from_nested_file(tmp_path: Path) -> None:
    outer = _project(tmp_path)
    inner = outer / "nested"
    inner.mkdir()
    (inner / "lakefile.toml").write_text('name = "Inner"\n', encoding="utf-8")
    (inner / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    source = inner / "src" / "Main.lean"
    source.parent.mkdir()
    source.write_text("theorem ok : True := by trivial\n", encoding="utf-8")

    result = inspect_project(source)
    assert result.project_root == "."
    assert result.lake is not None
    assert result.lake.name == "Inner"


def test_toml_depth_limit_is_independent_of_python_recursion_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    nested = "[" * 129 + "0" + "]" * 129
    (root / "lakefile.toml").write_text(
        f'name = "Example"\nvalue = {nested}\n', encoding="utf-8"
    )
    monkeypatch.setattr(sys, "getrecursionlimit", lambda: 10_000)
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-toml" for diagnostic in result.diagnostics)


def test_deep_toml_is_a_path_free_failure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nvalue = ' + "[" * 1500 + "0" + "]" * 1500 + "\n",
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-toml" for diagnostic in result.diagnostics)
    assert str(tmp_path) not in result.to_json()


def test_dotted_table_header_depth_is_bounded(tmp_path: Path) -> None:
    root = _project(tmp_path)
    header = "[" + ".".join(["a"] * 200) + "]"
    (root / "lakefile.toml").write_text(
        f'name = "Example"\n{header}\nvalue = 0\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-toml" for diagnostic in result.diagnostics)


def test_dotted_keys_within_the_limit_still_parse(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        "[leanOptions]\n"
        "weak.linter.mathlibStandardSet = true\n"
        '# a.b.c.d.e comment must not leak into the next line\n'
        "pp.unicode.fun = true\n",
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None and result.lake.name == "Example"


def test_multiline_string_terminator_cannot_hide_excessive_toml_depth(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    nested = "[" * 130 + "0" + "]" * 130
    (root / "lakefile.toml").write_text(
        'name = "Example"\nx = """abc""""\ny = ' + nested + "\n",
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-toml" for diagnostic in result.diagnostics)


def test_malformed_toml_is_a_path_free_failure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text("name = [\n", encoding="utf-8")

    result = inspect_project(root)
    assert not result.ok
    assert [diagnostic.code for diagnostic in result.diagnostics if diagnostic.severity == "error"] == [
        "invalid-lake-toml"
    ]
    assert str(tmp_path) not in result.to_json()


@pytest.mark.parametrize(
    "src_dir",
    [
        "../outside",
        "/absolute",
        "C:\\outside",
        "..\\outside",
        "foo\\..\\outside",
        "C:outside",
        "\\outside",
        "src\\lib",
    ],
)
def test_rejects_nonportable_lake_paths(tmp_path: Path, src_dir: str) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        f'name = "Example"\n[[lean_lib]]\nname = "Example"\nsrcDir = "{src_dir.replace(chr(92), chr(92) * 2)}"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "nonportable-lake-path" for diagnostic in result.diagnostics)


def test_parses_library_roots_and_executable_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nsrcDir = "pkg"\n'
        '[[lean_lib]]\nname = "Library"\nroots = ["A", "B.C"]\nsrcDir = "lib"\n'
        '[[lean_exe]]\nname = "Runner"\nroot = "Main"\nsrcDir = "exe"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == ("A", "B.C")
    assert result.lake.targets[0].root is None
    assert result.lake.targets[1].root == "Main"
    assert result.lake.targets[1].roots == ()


def test_default_target_roots_are_effective(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_lib]]\nname = "Library"\n'
        '[[lean_exe]]\nname = "Runner"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == ("Library",)
    assert result.lake.targets[1].root == "Runner"


def test_noncanonical_target_name_uses_lake_simple_name_fallback(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[lean_lib]]\nname = "my-module"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == ("«my-module»",)
    assert not any(
        diagnostic.code == "lake-target-names-indeterminate"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    ("name", "root"),
    [
        ("foo«", "«foo«»"),
        ("foo»", "foo»"),
        ("#foo", "#foo"),
        ("?foo", "?foo"),
        ("«#foo».«my-module»", "#foo.my-module"),
    ],
)
def test_target_name_rendering_matches_lake_escape_fallback(
    tmp_path: Path, name: str, root: str
) -> None:
    project = _project(tmp_path)
    (project / "lakefile.toml").write_text(
        f'name = "Example"\n[[lean_lib]]\nname = "{name}"\n', encoding="utf-8"
    )

    result = inspect_project(project)

    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == (root,)


@pytest.mark.parametrize(
    "version", ["wat", "v1.2.3", "1.2", "1.2.3.4", "1.2.3+build", "١.٢.٣"]
)
def test_invalid_lake_versions_are_rejected(tmp_path: Path, version: str) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        f'name = "Example"\nversion = "{version}"\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_prerelease_lake_version_is_accepted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nversion = "1.2.3-rc1"\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None and result.lake.version == "1.2.3-rc1"


def test_numeric_roots_are_canonicalized_before_duplicate_detection(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_exe]]\nname = "First"\nroot = "01"\n'
        '[[lean_exe]]\nname = "Second"\nroot = "1"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ('[[lean_lib]]\nname = "{numeric}"\n', ("1",)),
        ('[[lean_exe]]\nname = "Runner"\nroot = "{numeric}"\n', "1"),
    ],
)
def test_large_numeric_lean_names_are_normalized_lexically(
    tmp_path: Path, declaration: str, expected: str | tuple[str, ...]
) -> None:
    root = _project(tmp_path)
    numeric = "0" * 4_999 + "1"
    (root / "lakefile.toml").write_text(
        'name = "Example"\n' + declaration.format(numeric=numeric), encoding="utf-8"
    )

    result = inspect_project(root)

    assert result.ok
    assert result.lake is not None
    target = result.lake.targets[0]
    assert (target.roots if target.kind == "lean_lib" else target.root) == expected


def test_letter_like_unicode_target_names_are_canonical(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[lean_lib]]\nname = "Ω"\nroots = ["Ω.x₁", "α"]\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == ("Ω.x₁", "α")
    assert not any(
        diagnostic.code == "lake-target-names-indeterminate"
        for diagnostic in result.diagnostics
    )


def test_duplicate_target_names_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_lib]]\nname = "Duplicate"\n'
        '[[lean_exe]]\nname = "Duplicate"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_duplicate_simple_fallback_target_names_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_lib]]\nname = "my-module"\n'
        '[[lean_exe]]\nname = "«my-module»"\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_duplicate_executable_roots_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_exe]]\nname = "First"\nroot = "Main"\n'
        '[[lean_exe]]\nname = "Second"\nroot = "Main"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_duplicate_mathlib_requirements_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8")
        + '\n[[require]]\nname = "mathlib"\ngit = "https://example.com/mathlib4.git"\nrev = "v4.32.2"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(
        diagnostic.code == "duplicate-mathlib-requirement"
        for diagnostic in result.diagnostics
    )


def test_escaped_mathlib_requirement_matches_catalog(tmp_path: Path) -> None:
    recommended = load_release_catalog().recommended
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "«mathlib»"\n'
        f'git = "{recommended.mathlib.git}"\n'
        f'rev = "{recommended.mathlib.input_revision}"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text(
        f"{recommended.lean.toolchain}\n", encoding="utf-8"
    )

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert result.compatibility.release == recommended.id


def test_equivalent_mathlib_requirement_spellings_are_duplicates(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[require]]\nname = "mathlib"\nrev = "v4.33.1"\n'
        '[[require]]\nname = "«mathlib»"\nrev = "v4.33.1"\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(
        diagnostic.code == "duplicate-mathlib-requirement"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    "requirement",
    [
        'name = "mathlib"\ngit = "https://github.com/leanprover-community/mathlib4.git"\nscope = "leanprover-community"\nrev = "v4.32.2"',
        'name = "mathlib"\ngit = { url = "https://github.com/leanprover-community/mathlib4.git" }\nrev = "v4.32.2"',
    ],
)
def test_supported_mathlib_dependency_forms_match_catalog(
    tmp_path: Path, requirement: str
) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        f'name = "Example"\n[[require]]\n{requirement}\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert result.ok
    assert result.compatibility.status == "supported"


def test_explicit_empty_scope_is_lakes_default_scope(tmp_path: Path) -> None:
    recommended = load_release_catalog().recommended
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\nscope = ""\n'
        f'git = "{recommended.mathlib.git}"\n'
        f'rev = "{recommended.mathlib.input_revision}"\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "supported"


def test_lake_generated_scope_requirement_is_indeterminate_offline(tmp_path: Path) -> None:
    """Reservoir scope metadata is not proof of an exact Git source."""
    recommended = load_release_catalog().recommended
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        'version = "0.1.0"\n'
        'keywords = ["math"]\n'
        'defaultTargets = ["Example"]\n\n'
        "[leanOptions]\n"
        "pp.unicode.fun = true\n"
        "relaxedAutoImplicit = false\n"
        "weak.linter.mathlibStandardSet = true\n"
        "maxSynthPendingDepth = 3\n\n"
        "[[require]]\n"
        'name = "mathlib"\n'
        'scope = "leanprover-community"\n'
        f'rev = "{recommended.mathlib.input_revision}"\n\n'
        "[[lean_lib]]\n"
        'name = "Example"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text(f"{recommended.lean.toolchain}\n", encoding="utf-8")

    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert result.compatibility.release is None


def test_scoped_mathlib_requirement_uses_the_resolved_manifest(tmp_path: Path) -> None:
    recommended = load_release_catalog().recommended
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        'scope = "leanprover-community"\n'
        f'rev = "{recommended.mathlib.input_revision}"\n',
        encoding="utf-8",
    )
    _write_mathlib_manifest(root)

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert result.mathlib.resolved_revision == "905b95818eb32af7874a58b427f50c1711a5e96c"
    assert result.compatibility.status == "supported"


def test_stale_mathlib_manifest_controls_actual_compatibility(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_mathlib_manifest(
        root,
        input_revision="v4.31.0",
        resolved_revision="2" * 40,
    )

    result = inspect_project(root)

    assert result.compatibility.status != "supported"
    assert any(
        diagnostic.code == "mathlib-manifest-stale"
        for diagnostic in result.diagnostics
    )


def test_mathlib_manifest_subdirectory_is_not_the_catalog_release(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_mathlib_manifest(root, scope="", subdirectory="Mathlib")

    result = inspect_project(root)

    assert result.mathlib is not None
    assert result.mathlib.subdirectory == "Mathlib"
    assert result.compatibility.status == "unlisted"


@pytest.mark.parametrize(
    ("config_file", "manifest_file"),
    [
        ("alternate.lean", "lake-manifest.json"),
        ("lakefile.lean", "alternate-manifest.json"),
    ],
)
def test_noncanonical_mathlib_load_files_are_not_the_catalog_release(
    config_file: str,
    manifest_file: str | None,
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _write_mathlib_manifest(
        root,
        scope="",
        config_file=config_file,
        manifest_file=manifest_file,
    )

    result = inspect_project(root)

    assert result.mathlib is not None
    assert result.mathlib.config_file == config_file
    assert result.mathlib.manifest_file == manifest_file
    assert result.compatibility.status == "unlisted"


def test_explicit_null_manifest_file_uses_lake_default(tmp_path: Path) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["packages"][0]["manifestFile"] = None
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.mathlib is not None
    assert result.mathlib.manifest_file == "lake-manifest.json"
    assert result.compatibility.status == "supported"


@pytest.mark.parametrize("field", ["name", "lakeDir", "fixedToolchain"])
def test_null_root_manifest_defaults_match_lake(field: str, tmp_path: Path) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = None
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "supported"


def test_omitted_mathlib_config_file_uses_lake_default(tmp_path: Path) -> None:
    root = _project(tmp_path)
    payload = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
    payload["packages"][0].pop("configFile")
    (root / "lake-manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.mathlib is not None
    assert result.mathlib.config_file == "lakefile"
    assert result.compatibility.status == "unlisted"


@pytest.mark.parametrize(
    ("field", "expected", "status"),
    [
        ("scope", "", "supported"),
        ("configFile", "lakefile", "unlisted"),
    ],
)
def test_null_package_fields_use_lake_defaults(
    field: str, expected: str, status: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["packages"][0][field] = None
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert getattr(
        result.mathlib,
        "config_file" if field == "configFile" else field,
    ) == expected
    assert result.compatibility.status == status


def test_null_root_packages_are_an_empty_lake_manifest(tmp_path: Path) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["packages"] = None
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert not any(
        diagnostic.code == "invalid-lake-manifest"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize("field", ["configFile", "manifestFile", "subDir"])
def test_manifest_load_paths_cannot_escape_the_package(
    field: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    payload = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
    payload["packages"][0][field] = "../outside"
    (root / "lake-manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "invalid-lake-manifest"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize("value", ["lakefile.lean/", "dir/./lakefile.lean"])
def test_noncanonical_manifest_load_paths_cannot_match_the_catalog(
    value: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["packages"][0]["configFile"] = value
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "invalid-lake-manifest"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    ("resolved_revision", "status"),
    [
        ("v4.32.2", "unlisted"),
        ("905B95818EB32AF7874A58B427F50C1711A5E96C", "supported"),
    ],
)
def test_lake_valid_git_revisions_are_not_manifest_errors(
    resolved_revision: str, status: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    _write_mathlib_manifest(
        root,
        scope="",
        resolved_revision=resolved_revision,
    )

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == status
    assert result.mathlib is not None
    assert result.mathlib.resolved_revision == resolved_revision


@pytest.mark.parametrize(
    ("input_revision", "scope", "diagnostic"),
    [
        (
            "905b95818eb32af7874a58b427f50c1711a5e96c",
            "",
            "mathlib-input-revision-alias",
        ),
        ("v4.32.2", "another-scope", "mathlib-scope-alias"),
    ],
)
def test_materially_identical_mathlib_aliases_remain_supported(
    input_revision: str,
    scope: str,
    diagnostic: str,
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        + (f'scope = "{scope}"\n' if scope else "")
        + f'rev = "{input_revision}"\n',
        encoding="utf-8",
    )
    _write_mathlib_manifest(
        root,
        input_revision=input_revision,
        scope=scope,
    )

    result = inspect_project(root)

    assert result.compatibility.status == "supported"
    assert any(item.code == diagnostic for item in result.diagnostics)


def test_unused_mathlib_manifest_entry_is_not_compatibility_evidence(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text('name = "Example"\n', encoding="utf-8")

    result = inspect_project(root)

    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "mathlib-manifest-unused"
        for diagnostic in result.diagnostics
    )


def test_unevaluated_lakefile_lean_cannot_claim_manifest_compatibility(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    (root / "lakefile.lean").write_text("package Example\n", encoding="utf-8")

    result = inspect_project(root)

    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "mathlib-config-unevaluated"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_constants_in_manifest_are_rejected(
    constant: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text[:-1] + f', "ignored": {constant}' + "}", encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "invalid-lake-manifest"
        for diagnostic in result.diagnostics
    )


def test_mathlib_path_override_controls_actual_compatibility(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_mathlib_path_override(root)

    result = inspect_project(root)

    assert result.ok
    assert result.package_overrides_path == ".lake/package-overrides.json"
    assert result.package_overrides_sha256 is not None
    assert result.mathlib is not None
    assert result.mathlib.source == ".lake/package-overrides.json"
    assert result.mathlib.path == "vendor/fake-mathlib"
    assert result.mathlib.as_dict() == {
        "name": "mathlib",
        "scope": "",
        "package_type": "path",
        "git": None,
        "input_revision": None,
        "resolved_revision": None,
        "declared_revision": "v4.32.2",
        "subdirectory": None,
        "config_file": "lakefile.toml",
        "manifest_file": "lake-manifest.json",
        "path": "vendor/fake-mathlib",
        "source": ".lake/package-overrides.json",
    }
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "mathlib-overridden"
        for diagnostic in result.diagnostics
    )


def test_invalid_package_overrides_block_manifest_compatibility(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_mathlib_path_override(root, version="2.0.0")

    result = inspect_project(root)

    assert not result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "invalid-package-overrides"
        for diagnostic in result.diagnostics
    )


def test_git_override_controls_catalog_compatibility(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_mathlib_git_override(root)

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert result.mathlib.source == ".lake/package-overrides.json"
    assert result.compatibility.status == "supported"
    assert any(
        diagnostic.code == "mathlib-overridden"
        for diagnostic in result.diagnostics
    )


def test_null_override_packages_fall_back_to_the_root_manifest(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _write_mathlib_git_override(root)
    override = root / ".lake/package-overrides.json"
    payload = json.loads(override.read_text(encoding="utf-8"))
    payload["packages"] = None
    override.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert result.mathlib.source == "lake-manifest.json"
    assert result.compatibility.status == "supported"


def test_override_ignores_unrelated_root_manifest_fields(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_mathlib_git_override(root)
    override = root / ".lake/package-overrides.json"
    payload = json.loads(override.read_text(encoding="utf-8"))
    payload.update(
        {
            "name": ".",
            "lakeDir": None,
            "fixedToolchain": "ignored",
        }
    )
    override.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert result.mathlib.source == ".lake/package-overrides.json"
    assert result.compatibility.status == "supported"


def test_duplicate_override_uses_lakes_last_entry(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_mathlib_git_override(root)
    override = root / ".lake/package-overrides.json"
    payload = json.loads(override.read_text(encoding="utf-8"))
    payload["packages"].insert(
        0,
        {
            "name": "mathlib",
            "scope": "",
            "configFile": "lakefile.lean/",
            "manifestFile": None,
            "inherited": False,
            "type": "git",
            "url": "git@github.com:attacker/shadowed.git",
            "rev": "shadowed",
        },
    )
    override.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert result.mathlib.source == ".lake/package-overrides.json"
    assert result.compatibility.status == "supported"
    assert not any(
        diagnostic.code in {"invalid-mathlib-url", "invalid-package-overrides"}
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize("manifest_state", ["missing", "invalid"])
def test_override_cannot_certify_without_a_valid_root_manifest(
    manifest_state: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    _write_mathlib_git_override(root)
    manifest = root / "lake-manifest.json"
    if manifest_state == "missing":
        manifest.unlink()
    else:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["version"] = "2.0.0"
        manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert not any(
        diagnostic.code == "mathlib-overridden"
        for diagnostic in result.diagnostics
    )
    expected = (
        "missing-lake-manifest"
        if manifest_state == "missing"
        else "invalid-lake-manifest"
    )
    assert any(diagnostic.code == expected for diagnostic in result.diagnostics)


@pytest.mark.parametrize("name", [".", "..", " ", "/", "mathlib.", ".mathlib"])
def test_invalid_lake_manifest_root_names_are_rejected(
    name: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["name"] = name
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "invalid-lake-manifest"
        for diagnostic in result.diagnostics
    )


def test_anonymous_lake_manifest_root_name_is_accepted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["name"] = "[anonymous]"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "supported"


def test_unrelated_git_packages_do_not_receive_mathlib_url_policy(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["packages"].append(
        {
            "name": "other",
            "scope": "",
            "inherited": False,
            "type": "git",
            "url": "git@github.com:example/other.git",
            "rev": "2" * 40,
            "inputRev": "main",
        }
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "supported"
    assert not any(
        diagnostic.code in {"invalid-mathlib-url", "invalid-lake-manifest"}
        for diagnostic in result.diagnostics
    )


def test_duplicate_manifest_package_name_uses_lakes_last_entry(tmp_path: Path) -> None:
    root = _project(tmp_path)
    payload = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
    payload["packages"][0]["url"] = "git@github.com:attacker/shadowed.git"
    payload["packages"][0]["configFile"] = "lakefile.lean/"
    payload["packages"].append(
        {
            "name": "mathlib",
            "scope": "",
            "configFile": "lakefile.toml",
            "manifestFile": None,
            "inherited": True,
            "type": "path",
            "dir": "vendor/fake-mathlib",
        }
    )
    (root / "lake-manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert result.mathlib.package_type == "path"
    assert result.mathlib.path == "vendor/fake-mathlib"
    assert result.compatibility.status == "indeterminate"
    assert not any(
        diagnostic.code in {"invalid-mathlib-url", "invalid-lake-manifest"}
        for diagnostic in result.diagnostics
    )


def test_inherited_flag_does_not_change_materialized_mathlib_identity(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    payload = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
    payload["packages"][0]["inherited"] = True
    (root / "lake-manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.compatibility.status == "supported"


def test_unsupported_lake_manifest_schema_blocks_compatibility(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_mathlib_manifest(root, version="2.0.0")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "invalid-lake-manifest"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize("version", [5, 6, "0.6.0"])
def test_lake_supported_legacy_manifest_is_advisory(
    version: int | str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    _write_mathlib_manifest(root, version=version)

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "unsupported-lake-manifest"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize("root_path", ["", "."])
def test_optional_lake_root_paths_mean_the_project_root(
    tmp_path: Path, root_path: str
) -> None:
    recommended = load_release_catalog().recommended
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        f'name = "Example"\nsrcDir = "{root_path}"\n'
        '[[require]]\nname = "mathlib"\n'
        f'git = "{recommended.mathlib.git}"\n'
        f'rev = "{recommended.mathlib.input_revision}"\nsubDir = "{root_path}"\n'
        f'[[lean_lib]]\nname = "Example"\nsrcDir = "{root_path}"\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert result.ok
    assert result.lake is not None
    assert result.lake.package_src_dir == "."
    assert result.lake.targets[0].src_dir == "."
    assert result.compatibility.status == "supported"


def test_unusable_mathlib_scope_is_valid_but_indeterminate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").unlink()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        'scope = "not a scope/../elsewhere"\nrev = "v4.32.2"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


@pytest.mark.parametrize(
    "source",
    [
        'source = { type = "path", dir = "vendor/mathlib" }',
        'source = { type = "git", url = "https://github.com/leanprover-community/mathlib4.git", rev = "v4.32.2" }',
    ],
)
def test_generic_mathlib_sources_are_valid_but_indeterminate(
    tmp_path: Path, source: str
) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").unlink()
    (root / "lakefile.toml").write_text(
        f'name = "Example"\n[[require]]\nname = "mathlib"\n{source}\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


def test_mathlib_git_subdirectory_is_valid_but_indeterminate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").unlink()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        'rev = "v4.32.2"\nsubDir = "Mathlib"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


def test_malformed_requirements_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nrequire = ["mathlib"]\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_malformed_scope_requirement_subdirectory_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        'scope = "leanprover-community"\nrev = "v4.32.2"\nsubDir = []\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_missing_package_name_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text('version = "0.1.0"\n', encoding="utf-8")
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_path_precedes_mathlib_git_and_is_indeterminate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").unlink()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        'path = "vendor/mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        'rev = "v4.32.2"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


def test_credentialed_mathlib_url_is_rejected_and_redacted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8").replace(
            "https://github.com/", "https://secret@example.com/"
        ),
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert result.mathlib is None
    assert "secret" not in result.to_json()
    assert any(diagnostic.code == "credentialed-mathlib-url" for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    "git_source, secret",
    [
        ("/private/home/project/mathlib", "/private/home"),
        ("https://github.com:bad/mathlib4.git", "github.com:bad"),
        ("https://github.com/mathlib4.git?token=secret", "token=secret"),
    ],
)
def test_invalid_mathlib_sources_are_rejected_and_redacted(
    tmp_path: Path, git_source: str, secret: str
) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8").replace(
            "https://github.com/leanprover-community/mathlib4.git", git_source
        ),
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert result.mathlib is None
    assert secret not in result.to_json()
    assert any(diagnostic.code == "invalid-mathlib-url" for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    ("git_source", "message"),
    [
        ("https://example.com", "must identify a repository"),
        ("http://example.com/mathlib4", "must be credential-free HTTPS"),
    ],
)
def test_invalid_mathlib_url_diagnostics_identify_the_failure(
    git_source: str, message: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8").replace(
            "https://github.com/leanprover-community/mathlib4.git", git_source
        ),
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(
        diagnostic.code == "invalid-mathlib-url"
        and message in diagnostic.message
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize("source", ["manifest", "override"])
@pytest.mark.parametrize(
    "raw_url",
    [
        " https://github.com/leanprover-community/mathlib4",
        "https://github.com/leanprover-community/math\tlib4",
        "https://github.com/leanprover-community/mathlib4\n",
    ],
)
def test_unsafe_manifest_urls_cannot_normalize_into_catalog_matches(
    source: str, raw_url: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    if source == "override":
        _write_mathlib_git_override(root)
        manifest = root / ".lake/package-overrides.json"
    else:
        manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["packages"][0]["url"] = raw_url
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "invalid-mathlib-url"
        for diagnostic in result.diagnostics
    )


def test_other_canonical_mathlib_url_is_unlisted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["packages"][0]["url"] = "https://example.com/mathlib4"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "unlisted"


def test_unlisted_release_is_advisory(tmp_path: Path) -> None:
    root = _project(tmp_path, revision="v4.31.0")
    result = inspect_project(root)
    assert result.ok
    assert result.compatibility.status == "unlisted"
    assert any(diagnostic.code == "release-unlisted" for diagnostic in result.diagnostics)


def test_copied_projects_have_identical_json(tmp_path: Path) -> None:
    first = _project(tmp_path)
    second = tmp_path / "copy"
    shutil.copytree(first, second)
    assert inspect_project(first).to_json() == inspect_project(second).to_json()


def test_deep_lake_manifest_is_a_stable_error(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").write_text(
        "[" * 1500 + "0" + "]" * 1500, encoding="utf-8"
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-manifest" for diagnostic in result.diagnostics)
    assert str(tmp_path) not in result.to_json()


def test_human_output_composes_package_and_target_source_dirs(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nsrcDir = "pkg"\n'
        '[[require]]\nname = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        'rev = "v4.32.2"\n'
        '[[lean_lib]]\nname = "Library"\nroots = ["A"]\nsrcDir = "lib"\n',
        encoding="utf-8",
    )
    assert main(["project", "inspect", str(root)]) == 0
    captured = capsys.readouterr()
    assert "srcDir: pkg/lib, roots: A" in captured.out
    assert "Mathlib: mathlib v4.32.2 @ 905b95818eb32af7874a58b427f50c1711a5e96c" in captured.out
    assert (
        "subDir=., configFile=lakefile.lean, manifestFile=lake-manifest.json"
        in captured.out
    )


@pytest.mark.parametrize(
    ("unsafe", "escaped"),
    [("\u2028", "\\u2028"), ("\u202e", "\\u202e")],
)
def test_human_output_escapes_line_and_direction_controls(
    unsafe: str,
    escaped: str,
    tmp_path: Path,
    capsys,
) -> None:
    root = _project(tmp_path)
    manifest = root / "lake-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["packages"][0]["scope"] = f"trusted{unsafe}warning[fake]"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["project", "inspect", str(root)]) == 0
    captured = capsys.readouterr()

    assert unsafe not in captured.out
    assert unsafe not in captured.err
    assert f"trusted{escaped}warning[fake]/mathlib" in captured.out


@pytest.mark.parametrize(
    ("unsafe", "escaped"),
    [("\u0890", "\\u0890"), ("\U0001343f", "\\U0001343f")],
)
def test_human_output_escapes_nonprintable_codepoints_across_unicode_versions(
    unsafe: str,
    escaped: str,
) -> None:
    assert _human_text(f"before{unsafe}after") == f"before{escaped}after"


def test_lakefile_lean_is_never_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    marker = tmp_path / "executed"
    (root / "lakefile.lean").write_text(
        f'unsafe def attempt := IO.FS.writeFile "{marker}" "bad"\n', encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("offline inspection invoked a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None and result.lake.format == "lean"
    assert result.compatibility.status == "indeterminate"
    assert not marker.exists()


def test_lakefile_lean_takes_precedence_over_toml(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.lean").write_text("package Example\n", encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.lake is not None and result.lake.format == "lean"
    assert any(
        diagnostic.code == "unused-lakefile-toml"
        for diagnostic in result.diagnostics
    )


def test_case_variant_lakefile_lean_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "Lakefile.lean").write_text("package Example\n", encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "lake-config-case-alias"
        for diagnostic in result.diagnostics
    )


def test_case_variant_lakefile_alone_still_selects_the_project_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "Lakefile.lean").write_text("package Example\n", encoding="utf-8")

    result = inspect_project(root)

    assert result.project_root == "."
    assert any(
        diagnostic.code == "lake-config-case-alias"
        for diagnostic in result.diagnostics
    )
    assert not any(
        diagnostic.code == "project-not-found"
        for diagnostic in result.diagnostics
    )


def test_case_variant_lake_state_directory_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    override = root / ".Lake/package-overrides.json"
    override.parent.mkdir()
    override.write_text('{"schemaVersion":"1.2.0","packages":[]}\n')

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "lake-state-case-alias"
        for diagnostic in result.diagnostics
    )


def test_case_variant_package_overrides_file_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    override = root / ".lake/Package-Overrides.json"
    override.parent.mkdir()
    override.write_text('{"schemaVersion":"1.2.0","packages":[]}\n')

    result = inspect_project(root)

    assert not result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "lake-state-case-alias"
        and diagnostic.path == ".lake/Package-Overrides.json"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    ("exact", "alias", "code"),
    [
        ("lake-manifest.json", "Lake-Manifest.json", "lake-state-case-alias"),
        ("lean-toolchain", "Lean-Toolchain", "lean-toolchain-case-alias"),
    ],
)
def test_case_variant_root_decision_files_are_rejected(
    exact: str, alias: str, code: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    (root / exact).rename(root / alias)

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == code and diagnostic.path == alias
        for diagnostic in result.diagnostics
    )


def test_case_variant_autoform_scaffold_file_is_ignored(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "MkDocs.yml").write_text("site_name: Example\n", encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.autoform.mkdocs_path is None
    assert not any(
        diagnostic.code == "project-path-case-alias"
        for diagnostic in result.diagnostics
    )


def test_case_variant_autoform_workflow_is_ignored(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workflow = root / ".github/workflows/Autoform-Verify.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: ignored\n", encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.autoform.verification_workflow_path is None


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("lake-manifest.json", "lake-manifest-unreadable"),
        ("lean-toolchain", "lean-toolchain-unreadable"),
        ("lakefile.toml", "lake-config-unreadable"),
    ],
)
def test_stable_unreadable_files_are_not_reported_as_changing(
    relative: str,
    code: str,
    tmp_path: Path,
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read mode-zero files")
    root = _project(tmp_path)
    if relative == "lake-manifest.json":
        _write_mathlib_manifest(root)
    target = root / relative
    target.chmod(0)
    try:
        result = inspect_project(root)
    finally:
        target.chmod(0o644)

    assert not any(
        diagnostic.code == "project-changed-during-inspection"
        for diagnostic in result.diagnostics
    )
    assert any(diagnostic.code == code for diagnostic in result.diagnostics)


def test_fifo_lakefile_fails_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable")
    root = _project(tmp_path)
    (root / "lakefile.toml").unlink()
    os.mkfifo(root / "lakefile.toml")
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "lake-config-not-regular" for diagnostic in result.diagnostics)


def test_known_fifo_is_rejected_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable")
    from autoform_cli.project import inspect as inspect_module

    root = _project(tmp_path)
    (root / "lakefile.toml").unlink()
    os.mkfifo(root / "lakefile.toml")
    original_capture = inspect_module._capture_file

    def guarded_capture(*args, **kwargs):
        if args[1] == "lakefile.toml":
            raise AssertionError("inspection opened a known nonregular node")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(inspect_module, "_capture_file", guarded_capture)
    result = inspect_project(root)

    assert not result.ok
    assert any(
        diagnostic.code == "lake-config-not-regular"
        for diagnostic in result.diagnostics
    )


def test_rejects_broken_symlinked_lakefile(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").unlink()
    try:
        (root / "lakefile.toml").symlink_to(root / "missing.toml")
    except OSError:
        pytest.skip("symlinks are unavailable")
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "lake-config-is-symlink" for diagnostic in result.diagnostics)


def test_rejects_symlinked_decision_files(tmp_path: Path) -> None:
    root = _project(tmp_path)
    real = root / "real-toolchain"
    real.write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    (root / "lean-toolchain").unlink()
    try:
        (root / "lean-toolchain").symlink_to(real)
    except OSError:
        pytest.skip("symlinks are unavailable")

    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "lean-toolchain-is-symlink" for diagnostic in result.diagnostics)


def test_rejects_symlinked_project_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    link = tmp_path / "project-link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    result = inspect_project(link / "lakefile.toml")
    assert not result.ok
    assert any(diagnostic.code == "project-path-is-symlink" for diagnostic in result.diagnostics)


def test_rejects_target_below_symlinked_directory(tmp_path: Path) -> None:
    root = _project(tmp_path)
    real = root / "real-src"
    real.mkdir()
    source = real / "Main.lean"
    source.write_text("theorem ok : True := by trivial\n", encoding="utf-8")
    link = root / "src"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    result = inspect_project(link / "Main.lean")
    assert not result.ok
    assert any(diagnostic.code == "project-path-is-symlink" for diagnostic in result.diagnostics)


def test_json_catalog_failure_is_machine_readable(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import __main__ as cli
    from autoform_cli.project.catalog import ProjectCatalogError

    def invalid_catalog():
        raise ProjectCatalogError("internal details")

    monkeypatch.setattr(cli, "load_release_catalog", invalid_catalog)
    assert main(["project", "versions", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "error": {
            "code": "project-catalog-invalid",
            "message": "The bundled project release catalog is invalid.",
        },
        "ok": False,
    }
    assert captured.err == ""


def test_cli_outputs_stable_json_and_failures(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    assert main(["project", "inspect", str(root), "--json"]) == 0
    first = capsys.readouterr()
    assert json.loads(first.out)["ok"] is True
    assert first.err == ""

    assert main(["project", "versions", "--json"]) == 0
    versions = capsys.readouterr()
    assert json.loads(versions.out)["schema"] == RELEASE_CATALOG_SCHEMA
    assert versions.err == ""

    assert main(["project", "inspect", str(root / "missing"), "--json"]) == 1
    failure = capsys.readouterr()
    assert json.loads(failure.out)["diagnostics"][0]["code"] == "target-does-not-exist"
    assert failure.err == ""


def test_rejects_symlinked_scaffold_parent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "workflows").mkdir()
    github = root / ".github"
    try:
        github.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "scaffold-path-is-symlink" for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    "relative, node",
    [("blueprint", "file"), ("mkdocs.yml", "directory")],
)
def test_scaffold_paths_require_their_expected_node_type(
    tmp_path: Path, relative: str, node: str
) -> None:
    root = _project(tmp_path)
    if node == "file":
        (root / relative).write_text("not a scaffold\n", encoding="utf-8")
    else:
        (root / relative).mkdir()

    result = inspect_project(root)
    assert not result.ok
    assert result.autoform.detected is False
    assert any(
        diagnostic.code == "scaffold-path-unexpected-type" and diagnostic.path == relative
        for diagnostic in result.diagnostics
    )


def test_blueprint_detection_requires_exact_directory_spelling(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "Blueprint").mkdir()

    result = inspect_project(root)

    assert result.ok
    assert result.autoform.detected is False
    assert result.autoform.blueprint_path is None
    assert not any(
        diagnostic.code == "project-path-case-alias"
        for diagnostic in result.diagnostics
    )


def test_case_variant_blueprint_is_not_a_nested_project_marker(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    lean_library = root / "nested/Blueprint"
    lean_library.mkdir(parents=True)

    result = inspect_project(lean_library)

    assert result.ok
    assert result.lake is not None
    assert result.lake.name == "Example"
    assert result.autoform.detected is False


def test_decision_files_come_from_one_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent two-file update must not synthesize a supported release."""
    from autoform_cli.project import inspect as inspect_module

    root = _project(tmp_path)
    (root / "lean-toolchain").write_text(
        "leanprover/lean4:v4.31.0\n", encoding="utf-8"
    )
    original_capture = inspect_module._capture_file
    swapped = False

    def swapping_capture(*args, **kwargs):
        nonlocal swapped
        entry = original_capture(*args, **kwargs)
        if args[1] == "lakefile.toml" and not swapped:
            swapped = True
            lakefile = root / "lakefile.toml"
            lakefile.write_text(
                lakefile.read_text(encoding="utf-8").replace("v4.32.2", "v4.31.0"),
                encoding="utf-8",
            )
            (root / "lean-toolchain").write_text(
                "leanprover/lean4:v4.32.2\n", encoding="utf-8"
            )
        return entry

    monkeypatch.setattr(inspect_module, "_capture_file", swapping_capture)
    result = inspect_project(root)

    assert swapped
    assert result.compatibility.status == "supported"
    assert any(
        diagnostic.code == "mathlib-manifest-stale"
        for diagnostic in result.diagnostics
    )


def test_lakefile_precedence_comes_from_the_same_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli.project import inspect as inspect_module

    root = _project(tmp_path)
    (root / "lakefile.lean").write_text("package Example\n", encoding="utf-8")
    original_info = inspect_module._relative_info
    removed = False

    def removing_info(descriptor: int, relative: str):
        nonlocal removed
        info = original_info(descriptor, relative)
        if relative == "lakefile.lean" and not removed:
            removed = True
            (root / "lakefile.lean").unlink()
        return info

    monkeypatch.setattr(inspect_module, "_relative_info", removing_info)
    result = inspect_project(root)

    assert removed
    assert result.lake is not None
    assert result.lake.format == "toml"


def test_root_discovery_stays_bound_to_the_directory_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing the discovered root with a symlink must not redirect inspection."""
    from autoform_cli.project import inspect as inspect_module

    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lakefile.toml").write_text('name = "Outside"\n', encoding="utf-8")
    (outside / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    swapped = False

    def swap() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        root.rename(tmp_path / "moved")
        try:
            root.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable")

    original_status = inspect_module._relative_status
    original_resolve = Path.resolve

    def swapping_status(descriptor: int, relative: str) -> str:
        status = original_status(descriptor, relative)
        swap()
        return status

    def swapping_resolve(self: Path, *args, **kwargs) -> Path:
        if self == root:
            swap()
        return original_resolve(self, *args, **kwargs)

    # Whichever step discovery reaches first performs the swap: canonicalizing a
    # pathname after checking it would hand back the outside project instead.
    monkeypatch.setattr(inspect_module, "_relative_status", swapping_status)
    monkeypatch.setattr(Path, "resolve", swapping_resolve)
    result = inspect_project(root, catalog=load_release_catalog())
    assert swapped
    assert result.ok
    assert result.lake is not None and result.lake.name == "Example"


def test_root_discovery_resolves_parent_components_from_retained_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli.project import inspect as inspect_module

    base = tmp_path / "base"
    base.mkdir()
    _project(base)
    child = base / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_root = _project(outside)
    (outside_root / "lakefile.toml").write_text('name = "Outside"\n', encoding="utf-8")
    swapped = False
    original_open = inspect_module._open_directory

    def moving_open(name: str, parent_descriptor: int | None) -> int:
        nonlocal swapped
        descriptor = original_open(name, parent_descriptor)
        if name == "child" and not swapped:
            swapped = True
            child.rename(outside / "child")
        return descriptor

    monkeypatch.setattr(inspect_module, "_open_directory", moving_open)
    result = inspect_project(child / ".." / "project")

    assert swapped
    assert result.ok
    assert result.lake is not None and result.lake.name == "Example"


def test_non_ascii_digits_do_not_match_stable_toolchain_versions(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lean-toolchain").write_text(
        "leanprover/lean4:v١.٢.٣\n", encoding="utf-8"
    )

    result = inspect_project(root)

    assert result.ok
    assert result.lean is not None and result.lean.version is None
    assert any(
        diagnostic.code == "unrecognized-lean-toolchain"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\t", "\x7f"])
def test_toolchain_control_characters_are_rejected(
    control: str, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    (root / "lean-toolchain").write_bytes(
        f"leanprover/lean4:v4.32.2{control}\n".encode()
    )

    result = inspect_project(root)

    assert not result.ok
    assert result.lean is None
    assert any(
        diagnostic.code == "invalid-lean-toolchain"
        for diagnostic in result.diagnostics
    )


def test_c1_toolchain_control_character_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lean-toolchain").write_text(
        "leanprover/lean4:v4.32.2\u009b\n", encoding="utf-8"
    )

    result = inspect_project(root)

    assert not result.ok
    assert result.lean is None
    assert any(
        diagnostic.code == "invalid-lean-toolchain"
        for diagnostic in result.diagnostics
    )


def test_tilde_expansion_failure_is_a_stable_diagnostic() -> None:
    result = inspect_project("~autoform-user-that-does-not-exist/project")
    assert not result.ok
    assert result.diagnostics[0].code == "target-unreadable"


def test_named_user_home_is_rejected_without_account_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("pwd is POSIX-only")
    import pwd

    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda _name: (_ for _ in ()).throw(
            AssertionError("inspection performed account-service lookup")
        ),
    )

    result = inspect_project("~autoform-account-service-user/project")

    assert not result.ok
    assert result.diagnostics[0].code == "target-unreadable"


@pytest.mark.parametrize("target", ["\0", "\ud800"])
def test_malformed_target_is_a_stable_diagnostic(target: str) -> None:
    result = inspect_project(target)

    assert not result.ok
    assert result.diagnostics[0].code == "target-unreadable"


def test_platform_without_descriptor_hardening_fails_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli.project import inspect as inspect_module

    root = _project(tmp_path)
    monkeypatch.setattr(inspect_module.os, "supports_dir_fd", set())

    result = inspect_project(root)

    assert not result.ok
    assert result.diagnostics[0].code == "secure-file-inspection-unavailable"


def test_reports_git_metadata_without_invoking_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / ".git").mkdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("offline inspection invoked a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = inspect_project(root)
    assert result.git_path == ".git"


def test_inspection_does_not_write_project(tmp_path: Path) -> None:
    root = _project(tmp_path)
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    inspect_project(root)
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (root / ".lake").exists()
    assert not (root / ".git").exists()
