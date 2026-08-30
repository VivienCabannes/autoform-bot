from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


_TEMPLATE = Path("autoform_cli/templates/github/autoform_audit.py")


def _load_helper(repo_root: Path) -> ModuleType:
    path = repo_root / _TEMPLATE
    spec = importlib.util.spec_from_file_location("autoform_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@pytest.fixture
def helper(repo_root: Path) -> ModuleType:
    return _load_helper(repo_root)


def _metadata(module: str, *, declarations: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "decls": declarations if declarations is not None else {"proof": []},
            "directImports": [],
            "module": module,
            "references": {},
            "version": 5,
        },
        separators=(",", ":"),
    ).encode()


def _trace(module: str, package: str = "Fixture") -> bytes:
    return json.dumps(
        {
            "synthetic": False,
            "inputs": [
                ["Module.name: " + module, "hash"],
                ["Package.id?: (some " + package + ")", "hash"],
            ],
        },
        separators=(",", ":"),
    ).encode()


def _module_members(module: str, *, package: str = "Fixture") -> list[tuple[str, bytes]]:
    stem = "./lib/lean/" + module.replace(".", "/")
    return [
        (f"{stem}.ilean", _metadata(module)),
        (f"{stem}.olean", b"olean"),
        (f"{stem}.trace", _trace(module, package)),
    ]


def _archive(path: Path, members: list[tuple[str, bytes | None]]) -> Path:
    with tarfile.open(path, "w:gz") as packed:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "elsewhere"
                packed.addfile(info)
            else:
                info.size = len(content)
                packed.addfile(info, io.BytesIO(content))
    return path


def _blueprint(tmp_path: Path, name: str = "blueprint") -> Path:
    blueprint = tmp_path / name
    _write(blueprint / "roadmap/README.md", "# Fixture roadmap\n")
    return blueprint


def _blueprint_article(blueprint: Path, name: str, *metadata: str) -> Path:
    path = blueprint / "roadmap" / f"{name}.md"
    _write(
        path,
        "\n".join(["---", *metadata, "---", "", f"# {name.title()}", ""]) + "\n",
    )
    return path


def test_root_package_comes_from_top_level_evaluated_config(
    helper: ModuleType, tmp_path: Path
) -> None:
    config = tmp_path / "evaluated.toml"
    _write(
        config,
        'name = "RootPackage"\nversion = "0.1.0"\n\n[[lean_lib]]\nname = "TargetName"\n',
    )

    assert helper.root_package_from_config(config) == "RootPackage"


@pytest.mark.parametrize(
    "text",
    [
        "version = \"0.1.0\"\n",
        'name = "One"\nname = "Two"\n',
        'name = "bad name"\n',
        '[[lean_lib]]\nname = "OnlyTarget"\n',
    ],
)
def test_invalid_evaluated_config_fails_closed(
    helper: ModuleType, tmp_path: Path, text: str
) -> None:
    config = tmp_path / "evaluated.toml"
    _write(config, text)

    with pytest.raises(helper.AuditInputError, match="root package name|package name"):
        helper.root_package_from_config(config)


def test_archive_modules_are_sorted_and_probe_fails_on_zero_declarations(
    helper: ModuleType, tmp_path: Path
) -> None:
    archive = _archive(
        tmp_path / "root.tgz",
        [*_module_members("Fixture.Basic"), *_module_members("Fixture")],
    )

    modules = helper.modules_from_archive(archive, "Fixture")
    probe = helper.render_probe(modules)

    assert modules == ("Fixture", "Fixture.Basic")
    assert probe.startswith("import Fixture\nimport Fixture.Basic\n")
    assert 'throwError "kernel-trust audit found no root-package declarations"' in probe
    assert "info.isUnsafe || info.isPartial" in probe
    assert "Lean.collectAxioms" in probe


def test_blueprint_targets_use_canonical_graph_and_preserve_multiple_claims(
    helper: ModuleType, tmp_path: Path
) -> None:
    blueprint = _blueprint(tmp_path)
    _blueprint_article(
        blueprint,
        "local",
        "declaration: lemma",
        "lean: Fixture.first, Fixture.second",
    )
    _blueprint_article(
        blueprint,
        "upstream",
        "declaration: theorem",
        "mathlib: true",
        "mathlib_declaration: Nat.Prime, Nat.prime_def_lt",
        "mathlib_file: Mathlib/Data/Nat/Prime/Basic.lean",
    )

    targets = helper.targets_from_blueprint(blueprint)

    assert [
        (
            target.article_path,
            target.name,
            target.expected_kind,
            target.owner,
            target.expected_module,
        )
        for target in targets
    ] == [
        ("roadmap/local.md", "Fixture.first", "theorem", "root", None),
        ("roadmap/local.md", "Fixture.second", "theorem", "root", None),
        (
            "roadmap/upstream.md",
            "Nat.Prime",
            "theorem",
            "mathlib",
            "Mathlib.Data.Nat.Prime.Basic",
        ),
        (
            "roadmap/upstream.md",
            "Nat.prime_def_lt",
            "theorem",
            "mathlib",
            "Mathlib.Data.Nat.Prime.Basic",
        ),
    ]
    probe = helper.render_probe(
        ("Fixture",), targets, ("Mathlib.Data.Nat.Prime.Basic",)
    )
    assert "import Mathlib.Data.Nat.Prime.Basic" in probe
    assert "local declaration {declName} belongs to non-root module" in probe
    assert "Mathlib declaration {declName} is owned by root module" in probe
    assert "does not have expected kind {expectedKind}" in probe


def test_mathlib_artifacts_require_exact_package_trace(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = helper.BlueprintTarget(
        "roadmap/upstream.md",
        "Mathlib.Provenance.claim",
        "theorem",
        "mathlib",
        "Mathlib.Provenance",
    )
    ilean = tmp_path / "build/lib/lean/Mathlib/Provenance.ilean"
    _write(ilean, _metadata("Mathlib.Provenance").decode())
    _write(ilean.with_suffix(".olean"), "olean")
    _write(ilean.with_suffix(".trace"), _trace("Mathlib.Provenance", "counterfeit").decode())
    queried = subprocess.CompletedProcess(
        ["lake", "query"], 0, json.dumps(str(ilean)) + "\n", ""
    )
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: queried)

    with pytest.raises(helper.AuditInputError, match="root package 'mathlib'"):
        helper.mathlib_modules_from_lake(tmp_path, (target,))

    _write(ilean.with_suffix(".trace"), _trace("Mathlib.Provenance", "mathlib").decode())
    assert helper.mathlib_modules_from_lake(tmp_path, (target,)) == ("Mathlib.Provenance",)


def test_probe_rejects_mathlib_claim_without_validated_trace_module(helper: ModuleType) -> None:
    target = helper.BlueprintTarget(
        "roadmap/upstream.md",
        "Mathlib.Provenance.claim",
        "theorem",
        "mathlib",
        "Mathlib.Provenance",
    )

    with pytest.raises(helper.AuditInputError, match="lack build trace metadata"):
        helper.render_probe(("Fixture",), (target,))


def test_helper_remains_compatible_during_an_immutable_pin_upgrade(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autoform_cli.audit as audit_module
    import autoform_cli.lean as lean_module

    legacy = {
        "definition": frozenset({"def"}),
        "lemma": frozenset({"lemma", "theorem"}),
    }
    monkeypatch.delattr(lean_module, "declaration_kind")
    monkeypatch.delattr(lean_module, "mathlib_module_name")
    monkeypatch.setattr(audit_module, "_DECLARATION_KEYWORDS", legacy, raising=False)

    compatibility_helper = _load_helper(repo_root)

    assert compatibility_helper.declaration_kind("definition") == "def"
    assert compatibility_helper.declaration_kind("lemma") == "theorem"
    assert compatibility_helper.mathlib_module_name("Mathlib/Order/Basic.lean") == (
        "Mathlib.Order.Basic"
    )


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (("lean: Fixture.result",), "declaration intent is missing or unsupported"),
        (
            ("declaration: theorem", "statement: formalized"),
            "formalized local work has no lean declaration target",
        ),
        (
            (
                "declaration: theorem",
                "mathlib: true",
                "mathlib_file: Mathlib/Data/Nat/Prime/Basic.lean",
            ),
            "mathlib_declaration is missing",
        ),
        (
            (
                "declaration: theorem",
                "mathlib: true",
                "mathlib_declaration: Nat.Prime",
            ),
            "mathlib_file is missing",
        ),
        (
            (
                "declaration: theorem",
                "mathlib: true",
                "mathlib_declaration: Nat.Prime",
                "mathlib_file: Mathlib/../Fake.lean",
            ),
            "canonical Mathlib",
        ),
        (
            (
                "declaration: theorem",
                "mathlib: true",
                "mathlib_declaration: Totally.Fake",
                "mathlib_file: Totally/Fake.lean",
            ),
            "canonical Mathlib",
        ),
    ],
)
def test_invalid_blueprint_claims_fail_before_probe_generation(
    helper: ModuleType,
    tmp_path: Path,
    metadata: tuple[str, ...],
    message: str,
) -> None:
    blueprint = _blueprint(tmp_path)
    _blueprint_article(blueprint, "claim", *metadata)

    with pytest.raises(helper.AuditInputError, match=message):
        helper.targets_from_blueprint(blueprint)


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([], "contains no ILean artifacts"),
        ([("./lib/lean/Fixture.ilean", b"not json")], "malformed ILean metadata"),
        ([("../Fixture.ilean", _metadata("Fixture"))], "unsafe ILean archive member path"),
        ([("./lib/lean/Fixture.ilean", None)], "not a regular file"),
        (
            [
                *_module_members("Fixture"),
                ("./other/Fixture.ilean", _metadata("Fixture")),
                ("./other/Fixture.olean", b"olean"),
                ("./other/Fixture.trace", _trace("Fixture")),
            ],
            "duplicate ILean artifacts",
        ),
        (
            [("./lib/lean/Wrong.ilean", _metadata("Fixture"))],
            "does not match its archive path",
        ),
    ],
)
def test_archive_validation_fails_closed(
    helper: ModuleType,
    tmp_path: Path,
    members: list[tuple[str, bytes | None]],
    message: str,
) -> None:
    archive = _archive(tmp_path / "root.tgz", members)

    with pytest.raises(helper.AuditInputError, match=message):
        helper.modules_from_archive(archive, "Fixture")


def test_orphan_ilean_cannot_resolve_from_dependency(helper: ModuleType, tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "root.tgz",
        [("./lib/lean/Dependency.ilean", _metadata("Dependency"))],
    )

    with pytest.raises(helper.AuditInputError, match="no matching OLean"):
        helper.modules_from_archive(archive, "Fixture")


def test_dependency_trace_cannot_claim_root_ownership(helper: ModuleType, tmp_path: Path) -> None:
    archive = _archive(tmp_path / "root.tgz", _module_members("Dependency", package="Dependency"))

    with pytest.raises(helper.AuditInputError, match="does not identify root package"):
        helper.modules_from_archive(archive, "Fixture")


def test_duplicate_member_path_fails_closed(helper: ModuleType, tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "root.tgz",
        [
            ("./lib/lean/Fixture.ilean", _metadata("Fixture")),
            ("./lib/lean/Fixture.ilean", _metadata("Fixture")),
        ],
    )

    with pytest.raises(helper.AuditInputError, match="duplicate build archive member"):
        helper.modules_from_archive(archive, "Fixture")


def test_helper_runs_on_python_310(repo_root: Path, tmp_path: Path) -> None:
    python = shutil.which("python3.10")
    if python is None:
        pytest.skip("python3.10 is not installed")
    helper_path = repo_root / _TEMPLATE
    environment = {**os.environ, "PYTHONPATH": str(repo_root)}
    config = tmp_path / "evaluated.toml"
    _write(config, 'name = "Fixture"\n')
    identified = subprocess.run(
        [python, str(helper_path), "--root-package", str(config)],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert identified.returncode == 0, identified.stderr
    assert identified.stdout == "Fixture\n"

    archive = _archive(tmp_path / "root.tgz", _module_members("Fixture"))
    blueprint = _blueprint(tmp_path)
    probe = tmp_path / "probe.lean"
    result = subprocess.run(
        [
            python,
            str(helper_path),
            "Fixture",
            str(archive),
            str(blueprint),
            str(tmp_path),
            str(probe),
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "prepared artifact audit for 1 root-package module" in result.stdout
    assert probe.is_file()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(
    project: Path, *command: str, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=project, capture_output=True, text=True, timeout=timeout
    )


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_toml_build_uses_target_src_dir_globs_and_import_closure(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "toml-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.toml",
        '''name = "TomlFixture"
version = "0.1.0"
defaultTargets = ["runner"]
srcDir = "package-src"

[[lean_lib]]
name = "Chosen"
srcDir = "library-src"
globs = ["Chosen.+"]

[[lean_exe]]
name = "runner"
root = "Main"
srcDir = "app-src"
''',
    )
    _write(project / "package-src/library-src/Chosen/Entry.lean", "import Chosen.Helper\n")
    _write(
        project / "package-src/library-src/Chosen/Helper.lean",
        "theorem helper_ok : True := by trivial\n",
    )
    _write(
        project / "package-src/library-src/Outside.lean",
        "theorem omitted : True := by trivial\n",
    )
    _write(
        project / "package-src/app-src/Main.lean",
        "import Chosen.Entry\ndef main : IO Unit := pure ()\n",
    )
    _write(project / "package-src/PackageOnly.lean", "theorem package_only : True := by trivial\n")

    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr

    modules = helper.modules_from_archive(archive, "TomlFixture")
    assert modules == ("Chosen.Entry", "Chosen.Helper", "Main")
    assert "Outside" not in modules
    assert "PackageOnly" not in modules

    probe = project / "probe.lean"
    probe.write_text(helper.render_probe(modules), encoding="utf-8")
    audited = _run(project, "lake", "env", "lean", str(probe))
    assert audited.returncode == 0, audited.stdout + audited.stderr


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_probe_binds_blueprint_claims_to_modules_and_kinds(
    helper: ModuleType, tmp_path: Path
) -> None:
    mathlib = tmp_path / "mathlib"
    mathlib.mkdir()
    _write(mathlib / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        mathlib / "lakefile.toml",
        '''name = "mathlib"
version = "0.1.0"
defaultTargets = ["Mathlib"]

[[lean_lib]]
name = "Mathlib"
''',
    )
    _write(
        mathlib / "Mathlib.lean",
        "import Mathlib.Genuine\nimport Mathlib.Actual\nimport Mathlib.Expected\n",
    )
    _write(
        mathlib / "Mathlib/Genuine.lean",
        "theorem Mathlib.Genuine.first : True := by trivial\n"
        "theorem Mathlib.Genuine.second : True := by trivial\n"
        "axiom Mathlib.Genuine.assumed : True\n",
    )
    _write(
        mathlib / "Mathlib/Actual.lean",
        "theorem Mathlib.Actual.claim : True := by trivial\n",
    )
    _write(mathlib / "Mathlib/Expected.lean", "import Mathlib.Actual\n")

    project = tmp_path / "project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.toml",
        '''name = "ArtifactFixture"
version = "0.1.0"
defaultTargets = ["Fixture"]

[[require]]
name = "mathlib"
path = "../mathlib"

[[lean_lib]]
name = "Fixture"
''',
    )
    _write(
        project / "Fixture.lean",
        "import Mathlib.Genuine\n"
        "import Mathlib.Expected\n"
        "theorem Fixture.first : True := by trivial\n"
        "theorem Fixture.second : True := by trivial\n"
        "def Fixture.value : Nat := 1\n"
        "abbrev Fixture.Count := Nat\n"
        "opaque Fixture.hidden : Nat := 0\n"
        "structure Fixture.Record where\n  value : Nat\n"
        "class Fixture.Marker where\n  token : Nat\n"
        "inductive Fixture.Flag where\n  | on\n"
        "instance Fixture.flagInhabited : Inhabited Fixture.Flag := ⟨.on⟩\n",
    )
    _write(project / "Scratch.lean", "theorem Fixture.unbuilt : True := by trivial\n")

    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr
    modules = helper.modules_from_archive(archive, "ArtifactFixture")
    assert modules == ("Fixture",)

    def audit_claims(name: str, articles: tuple[tuple[str, ...], ...]):
        blueprint = _blueprint(tmp_path, f"blueprint-{name}")
        for index, metadata in enumerate(articles):
            _blueprint_article(blueprint, f"claim-{index}", *metadata)
        probe = project / f"probe-{name}.lean"
        targets = helper.targets_from_blueprint(blueprint)
        mathlib_modules = helper.mathlib_modules_from_lake(project, targets)
        probe.write_text(
            helper.render_probe(modules, targets, mathlib_modules), encoding="utf-8"
        )
        return _run(project, "lake", "env", "lean", str(probe))

    positive = audit_claims(
        "positive",
        (
            (
                "declaration: theorem",
                "lean: Fixture.first, Fixture.second",
            ),
            (
                "declaration: theorem",
                "mathlib: true",
                "mathlib_declaration: Mathlib.Genuine.first, Mathlib.Genuine.second",
                "mathlib_file: Mathlib/Genuine.lean",
            ),
            (
                "declaration: axiom",
                "mathlib: true",
                "mathlib_declaration: Mathlib.Genuine.assumed",
                "mathlib_file: Mathlib/Genuine.lean",
            ),
            ("declaration: definition", "lean: Fixture.value"),
            ("declaration: abbrev", "lean: Fixture.Count"),
            ("declaration: opaque", "lean: Fixture.hidden"),
            ("declaration: structure", "lean: Fixture.Record"),
            ("declaration: class", "lean: Fixture.Marker"),
            ("declaration: inductive", "lean: Fixture.Flag"),
            ("declaration: instance", "lean: Fixture.flagInhabited"),
        ),
    )
    assert positive.returncode == 0, positive.stdout + positive.stderr

    failures = {
        "wrong-kind": (
            ("declaration: theorem", "lean: Fixture.value"),
            "does not have expected kind theorem",
        ),
        "unbuilt": (
            ("declaration: theorem", "lean: Fixture.unbuilt"),
            "local declaration does not exist",
        ),
        "wrong-owner": (
            ("declaration: theorem", "lean: Mathlib.Genuine.first"),
            "belongs to non-root module Mathlib.Genuine",
        ),
        "missing-mathlib": (
            (
                "declaration: theorem",
                "mathlib: true",
                "mathlib_declaration: Mathlib.Genuine.missing",
                "mathlib_file: Mathlib/Genuine.lean",
            ),
            "Mathlib declaration does not exist",
        ),
        "wrong-module": (
            (
                "declaration: theorem",
                "mathlib: true",
                "mathlib_declaration: Mathlib.Actual.claim",
                "mathlib_file: Mathlib/Expected.lean",
            ),
            "belongs to Mathlib.Actual, not Mathlib.Expected",
        ),
    }
    rejected = audit_claims(
        "rejected",
        tuple(metadata for metadata, _message in failures.values()),
    )
    output = rejected.stdout + rejected.stderr
    assert rejected.returncode != 0, output
    for name, (_metadata, message) in failures.items():
        assert message in output, f"{name}: {output}"


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_mathlib_claim_requires_exact_lake_package_id(
    helper: ModuleType, tmp_path: Path
) -> None:
    def build_case(case_name: str, package_id: str) -> tuple[Path, tuple[str, ...], tuple[object, ...]]:
        case = tmp_path / case_name
        dependency = case / "dependency"
        dependency.mkdir(parents=True)
        _write(dependency / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
        _write(
            dependency / "lakefile.toml",
            f'''name = "{package_id}"
version = "0.1.0"
defaultTargets = ["Mathlib"]

[[lean_lib]]
name = "Mathlib"
''',
        )
        _write(
            dependency / "Mathlib.lean",
            "import Mathlib.Provenance\n",
        )
        _write(
            dependency / "Mathlib/Provenance.lean",
            "theorem Mathlib.Provenance.claim : True := by trivial\n",
        )

        project = case / "project"
        project.mkdir()
        _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
        _write(
            project / "lakefile.toml",
            f'''name = "{case_name}Root"
version = "0.1.0"
defaultTargets = ["Fixture"]

[[require]]
name = "{package_id}"
path = "../dependency"

[[lean_lib]]
name = "Fixture"
''',
        )
        _write(
            project / "Fixture.lean",
            "import Mathlib.Provenance\n"
            f"theorem {case_name}Root.claim : True := by trivial\n",
        )
        built = _run(project, "lake", "build")
        assert built.returncode == 0, built.stdout + built.stderr
        archive = project / "root.tgz"
        packed = _run(project, "lake", "pack", str(archive))
        assert packed.returncode == 0, packed.stdout + packed.stderr

        blueprint = _blueprint(case)
        _blueprint_article(
            blueprint,
            "upstream",
            "declaration: theorem",
            "mathlib: true",
            "mathlib_declaration: Mathlib.Provenance.claim",
            "mathlib_file: Mathlib/Provenance.lean",
        )
        modules = helper.modules_from_archive(archive, f"{case_name}Root")
        targets = helper.targets_from_blueprint(blueprint)
        return project, modules, targets

    counterfeit_project, _counterfeit_modules, counterfeit_targets = build_case(
        "Counterfeit", "counterfeit"
    )
    with pytest.raises(helper.AuditInputError, match="package id 'mathlib'"):
        helper.mathlib_modules_from_lake(counterfeit_project, counterfeit_targets)

    mathlib_project, mathlib_root_modules, mathlib_targets = build_case("Mathlib", "mathlib")
    mathlib_modules = helper.mathlib_modules_from_lake(mathlib_project, mathlib_targets)
    assert mathlib_modules == ("Mathlib.Provenance",)
    probe = mathlib_project / "probe.lean"
    probe.write_text(
        helper.render_probe(mathlib_root_modules, mathlib_targets, mathlib_modules),
        encoding="utf-8",
    )
    audited = _run(mathlib_project, "lake", "env", "lean", str(probe))
    assert audited.returncode == 0, audited.stdout + audited.stderr


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_root_package_clean_excludes_stale_custom_artifacts(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "stale-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.lean",
        '''import Lake
open Lake DSL
package «StaleFixture» where
  buildDir := "custom-output"
@[default_target]
lean_lib «Fresh»
''',
    )
    _write(project / "Fresh.lean", "theorem fresh_ok : True := by trivial\n")
    stale = project / "custom-output/lib/lean"
    _write(stale / "Stale.ilean", _metadata("Stale").decode())
    _write(stale / "Stale.olean", "stale")
    _write(stale / "Stale.trace", _trace("Stale", "StaleFixture").decode())

    loaded = _run(project, "lake", "env", "true")
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    cleaned = _run(project, "lake", "clean", "StaleFixture")
    assert cleaned.returncode == 0, cleaned.stdout + cleaned.stderr
    assert not (project / "custom-output").exists()
    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr

    assert helper.modules_from_archive(archive, "StaleFixture") == ("Fresh",)


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_lean_manifest_supports_custom_build_dir(helper: ModuleType, tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    _write(dependency / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        dependency / "lakefile.lean",
        '''import Lake
open Lake DSL
package «Dependency»
lean_lib «Dependency»
''',
    )
    _write(dependency / "Dependency.lean", "theorem dependency_ok : True := by trivial\n")

    project = tmp_path / "lean-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.lean",
        '''import Lake
open Lake DSL

package «LeanFixture» where
  buildDir := "custom-output"
  srcDir := "package-src"

require «Dependency» from "../dependency"

@[default_target]
lean_lib «PublicApi» where
  srcDir := "sources"
  globs := #[.submodules `PublicApi]
''',
    )
    _write(
        project / "package-src/sources/PublicApi/Entry.lean",
        "import PublicApi.Internal\nimport Dependency\n",
    )
    _write(
        project / "package-src/sources/PublicApi/Internal.lean",
        "theorem internal_ok : True := by trivial\n",
    )
    _write(
        project / "package-src/sources/Outside.lean",
        "theorem omitted : True := by trivial\n",
    )

    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr

    assert (project / "custom-output/lib/lean/PublicApi/Entry.ilean").is_file()
    modules = helper.modules_from_archive(archive, "LeanFixture")
    assert modules == ("PublicApi.Entry", "PublicApi.Internal")
    assert "Dependency" not in modules

    probe = project / "probe.lean"
    probe.write_text(helper.render_probe(modules), encoding="utf-8")
    audited = _run(project, "lake", "env", "lean", str(probe))
    assert audited.returncode == 0, audited.stdout + audited.stderr


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_bundled_project_builds_against_pinned_mathlib(
    repo_root: Path, tmp_path: Path
) -> None:
    source = repo_root / "skills/setup/assets/cabannes-thesis-project"
    project = tmp_path / "cabannes-thesis-project"
    shutil.copytree(
        source,
        project,
        ignore=shutil.ignore_patterns(".lake", "site", "site-src", "__pycache__"),
    )

    cached = _run(project, "lake", "exe", "cache", "get", timeout=900)
    assert cached.returncode == 0, cached.stdout + cached.stderr
    built = _run(project, "lake", "build", "CabannesThesis", timeout=900)

    assert built.returncode == 0, built.stdout + built.stderr
    assert (project / ".lake/packages/mathlib/Mathlib.lean").is_file()
    assert (project / ".lake/build/lib/lean/CabannesThesis.olean").is_file()

    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr
    probe = project / "artifact-probe.lean"
    generated = _run(
        project,
        sys.executable,
        str(project / ".github/autoform_audit.py"),
        "CabannesThesis",
        str(archive),
        str(project / "blueprint"),
        str(project),
        str(probe),
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    audited = _run(project, "lake", "env", "lean", str(probe), timeout=900)
    assert audited.returncode == 0, audited.stdout + audited.stderr


def test_example_and_template_helpers_are_identical(repo_root: Path) -> None:
    template = repo_root / _TEMPLATE
    example = repo_root / "skills/setup/assets/cabannes-thesis-project/.github/autoform_audit.py"

    assert example.read_bytes() == template.read_bytes()
