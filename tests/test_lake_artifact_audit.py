from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from pathlib import PurePosixPath
from types import ModuleType

import pytest

from autoform_cli import artifact_audit as artifact_audit_module


_TEMPLATE = Path("autoform_cli/templates/github/autoform_audit.py")


@pytest.fixture
def helper() -> ModuleType:
    return artifact_audit_module


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


def _blueprint(tmp_path: Path) -> Path:
    blueprint = tmp_path / "blueprint"
    _write(blueprint / "roadmap/README.md", "# Fixture roadmap\n")
    return blueprint


def _article(blueprint: Path, name: str, *metadata: str) -> None:
    _write(
        blueprint / "roadmap" / f"{name}.md",
        "\n".join(("---", *metadata, "---", "", f"# {name.title()}", "")) + "\n",
    )


def _built_local_audit_fixture(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.toml",
        'name = "Fixture"\nversion = "0.1.0"\ndefaultTargets = ["Fixture"]\n\n'
        '[[lean_lib]]\nname = "Fixture"\n',
    )
    _write(project / "Fixture.lean", "theorem Fixture.claim : True := by trivial\n")
    blueprint = _blueprint(project)
    _article(blueprint, "claim", "declaration: theorem", "lean: Fixture.claim")
    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    return project, blueprint


def test_root_package_comes_from_top_level_evaluated_config(
    helper: ModuleType, tmp_path: Path
) -> None:
    config = tmp_path / "evaluated.toml"
    _write(
        config,
        'name = "RootPackage"\nversion = "0.1.0"\n\n[[lean_lib]]\nname = "TargetName"\n',
    )

    assert helper.root_package_from_config(config) == "RootPackage"


def test_root_package_accepts_canonical_quoted_lean_names(
    helper: ModuleType, tmp_path: Path
) -> None:
    config = tmp_path / "evaluated.toml"
    _write(config, 'name = "«formal-math»"\n')

    assert helper.root_package_from_config(config) == "«formal-math»"


@pytest.mark.parametrize(
    "text",
    [
        "version = \"0.1.0\"\n",
        'name = "One"\nname = "Two"\n',
        'name = "bad name"\n',
        'name = "--help"\n',
        'name = "bad/name"\n',
        'name = "Trailing."\n',
        'name = "«unterminated"\n',
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


def test_evaluated_config_rejects_kernel_check_bypass(
    helper: ModuleType, tmp_path: Path
) -> None:
    config = tmp_path / "evaluated.toml"
    _write(
        config,
        'name = "Fixture"\n\n[leanOptions]\ndebug.skipKernelTC = true\n',
    )

    with pytest.raises(helper.AuditInputError, match="debug.skipKernelTC"):
        helper.root_package_from_config(config)


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_dynamically_constructed_kernel_bypass_survives_lake_evaluation_and_is_rejected(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "dynamic-option"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    source = '''import Lake
open Lake DSL

def kernelOption := Lean.Name.mkStr2 "debug" "skipKernelTC"

package «DynamicOption» where
  leanOptions := #[⟨kernelOption, true⟩]

lean_lib «DynamicOption»
'''
    assert "debug.skipKernelTC" not in source
    _write(project / "lakefile.lean", source)
    config = project / "evaluated.toml"
    translated = _run(project, "lake", "translate-config", "toml", str(config))
    assert translated.returncode == 0, translated.stdout + translated.stderr
    assert "debug.skipKernelTC" in config.read_text(encoding="utf-8")

    with pytest.raises(helper.AuditInputError, match="debug.skipKernelTC"):
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


def test_blueprint_targets_bind_every_local_claim(helper: ModuleType, tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    _article(blueprint, "local", "declaration: lemma", "lean: Fixture.first, Fixture.second")

    targets = helper.targets_from_blueprint(blueprint)

    assert [(target.article_path, target.name, target.expected_kind) for target in targets] == [
        ("roadmap/local.md", "Fixture.first", "theorem"),
        ("roadmap/local.md", "Fixture.second", "theorem"),
    ]
    probe = helper.render_probe(("Fixture",), targets)
    assert "belongs to non-root module" in probe
    assert "does not have expected kind" in probe
    assert helper.preflight_blueprint(blueprint) == 2


def test_mathlib_claim_is_bound_to_its_declared_source_module(
    helper: ModuleType, tmp_path: Path
) -> None:
    blueprint = _blueprint(tmp_path)
    _article(
        blueprint,
        "upstream",
        "declaration: theorem",
        "mathlib: true",
        "mathlib_declaration: Nat.Prime",
        "mathlib_file: Mathlib/Data/Nat/Prime/Defs.lean",
    )

    targets = helper.targets_from_blueprint(blueprint)

    assert targets == (
        helper.BlueprintTarget(
            "roadmap/upstream.md",
            "Nat.Prime",
            "theorem",
            owner="mathlib",
            expected_module="Mathlib.Data.Nat.Prime.Defs",
            source_file="Mathlib/Data/Nat/Prime/Defs.lean",
        ),
    )
    assert helper.preflight_blueprint(blueprint) == 1
    with pytest.raises(helper.AuditInputError, match="Lean project root"):
        helper.run_artifact_audit(blueprint, tmp_path / "missing-project")


@pytest.mark.parametrize(
    ("version_field", "scope", "url"),
    [
        ("version", "", "https://github.com/leanprover-community/mathlib4.git"),
        ("version", "leanprover-community", "https://github.com/leanprover-community/mathlib4"),
        ("schemaVersion", None, "https://github.com/leanprover-community/mathlib4.git"),
    ],
)
def test_mathlib_manifest_accepts_official_modern_variants(
    helper: ModuleType,
    version_field: str,
    scope: str | None,
    url: str,
) -> None:
    entry: dict[str, object] = {
        "name": "mathlib",
        "type": "git",
        "url": url,
        "rev": "a" * 40,
        "subDir": None,
    }
    if scope is not None:
        entry["scope"] = scope
    manifest = {
        version_field: "1.2.0" if version_field == "version" else "0.7.1",
        "packagesDir": ".lake/packages",
        "packages": [entry],
    }

    parsed = helper._mathlib_manifest_from_bytes(json.dumps(manifest).encode())

    assert parsed.packages_dir.as_posix() == ".lake/packages"
    assert parsed.revision == "a" * 40


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("legacy-version", "unsupported Lake manifest schema version"),
        ("two-versions", "exactly one version or schemaVersion"),
        ("path", "Git dependency"),
        ("mirror", "canonical mathlib4 repository"),
        ("uppercase-revision", "full lowercase 40-hex commit"),
        ("subdirectory", "must not select a subdirectory"),
        ("scope", "unsupported package scope"),
        ("duplicate", "exactly one mathlib"),
        ("packages-escape", "canonical confined relative path"),
    ],
)
def test_mathlib_manifest_rejects_ambiguous_or_noncanonical_provenance(
    helper: ModuleType,
    mutation: str,
    message: str,
) -> None:
    entry: dict[str, object] = {
        "name": "mathlib",
        "type": "git",
        "url": "https://github.com/leanprover-community/mathlib4",
        "rev": "a" * 40,
        "subDir": None,
        "scope": "leanprover-community",
    }
    manifest: dict[str, object] = {
        "version": "1.2.0",
        "packagesDir": ".lake/packages",
        "packages": [entry],
    }
    if mutation == "legacy-version":
        manifest["version"] = "0.6.0"
    elif mutation == "two-versions":
        manifest["schemaVersion"] = "1.2.0"
    elif mutation == "path":
        entry["type"] = "path"
    elif mutation == "mirror":
        entry["url"] = "https://example.invalid/mathlib4.git"
    elif mutation == "uppercase-revision":
        entry["rev"] = "A" * 40
    elif mutation == "subdirectory":
        entry["subDir"] = "Mathlib"
    elif mutation == "scope":
        entry["scope"] = "counterfeit"
    elif mutation == "duplicate":
        manifest["packages"] = [entry, dict(entry)]
    elif mutation == "packages-escape":
        manifest["packagesDir"] = "../packages"

    with pytest.raises(helper.AuditInputError, match=message):
        helper._mathlib_manifest_from_bytes(json.dumps(manifest).encode())


def test_project_input_capture_prunes_custom_manifest_packages_directory(
    helper: ModuleType, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    _write(project_path / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(project_path / "lakefile.toml", 'name = "Fixture"\n')
    _write(project_path / "Root.lean", "theorem Root.claim : True := by trivial\n")
    manifest = {
        "version": "1.2.0",
        "packagesDir": "vendor/packages",
        "packages": [],
    }
    _write(project_path / "lake-manifest.json", json.dumps(manifest))
    _write(
        project_path / "vendor/packages/mathlib/Counterfeit.lean",
        "theorem Counterfeit.claim : False := by sorry\n",
    )
    _write(project_path / "vendor/packages/mathlib/.git/objects/large", "x" * 1024)
    project = helper._open_project_tree(project_path)
    try:
        snapshot = helper._capture_project_inputs(project)
    finally:
        project.close()

    assert {path for path, _digest in snapshot.files} == {
        "Root.lean",
        "lake-manifest.json",
        "lakefile.toml",
        "lean-toolchain",
    }


def test_mathlib_checkout_and_source_are_bound_to_manifest_commit(
    helper: ModuleType, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    checkout_path = project_path / ".lake/packages/mathlib"
    _write(checkout_path / ".gitignore", "/.lake/\n")
    _write(
        checkout_path / "Mathlib/Provenance.lean",
        "theorem Mathlib.Provenance.claim : True := by trivial\n",
    )
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.name", "Autoform Test"),
        ("git", "config", "user.email", "autoform@example.invalid"),
        ("git", "add", ".gitignore", "Mathlib/Provenance.lean"),
        ("git", "commit", "-q", "-m", "fixture"),
    ):
        result = _run(checkout_path, *command)
        assert result.returncode == 0, result.stdout + result.stderr
    revision = _run(checkout_path, "git", "rev-parse", "HEAD").stdout.strip()
    manifest = helper._MathlibManifest(PurePosixPath(".lake/packages"), revision)
    project = helper._open_project_tree(project_path)
    checkout = helper._open_mathlib_checkout(project, manifest)
    try:
        helper._validate_mathlib_checkout(
            checkout,
            manifest,
            helper._Deadline.after(10),
            environment=os.environ,
        )
        snapshot = helper._capture_mathlib_sources(
            checkout,
            ("Mathlib/Provenance.lean",),
            revision,
            helper._Deadline.after(10),
            environment=os.environ,
        )
        assert snapshot.digests[0][0] == "Mathlib/Provenance.lean"
        _write(checkout_path / ".lake/build/cache-entry", "generated\n")
        after_build_output = helper._capture_mathlib_sources(
            checkout,
            ("Mathlib/Provenance.lean",),
            revision,
            helper._Deadline.after(10),
            environment=os.environ,
        )
        assert after_build_output == snapshot

        _write(
            checkout_path / "Mathlib/Provenance.lean",
            "theorem Mathlib.Provenance.counterfeit : False := by sorry\n",
        )
        with pytest.raises(helper.AuditInputError, match="does not match its blob"):
            helper._capture_mathlib_sources(
                checkout,
                ("Mathlib/Provenance.lean",),
                revision,
                helper._Deadline.after(10),
                environment=os.environ,
            )
        with pytest.raises(helper.AuditInputError, match="tracked or visible untracked changes"):
            helper._validate_mathlib_checkout(
                checkout,
                manifest,
                helper._Deadline.after(10),
                environment=os.environ,
            )
        restored = _run(checkout_path, "git", "checkout", "--", "Mathlib/Provenance.lean")
        assert restored.returncode == 0, restored.stderr
        _write(checkout_path / ".git/info/exclude", "Mathlib/Hidden.lean\n")
        _write(checkout_path / "Mathlib/Hidden.lean", "theorem hidden : True := by trivial\n")
        with pytest.raises(helper.AuditInputError, match="outside build roots"):
            helper._validate_mathlib_checkout(
                checkout,
                manifest,
                helper._Deadline.after(10),
                environment=os.environ,
                allowed_build_roots=(PurePosixPath(".lake/build"),),
            )
    finally:
        checkout.close()
        project.close()


def test_mathlib_checkout_rejects_unsafe_git_configuration_before_status(
    helper: ModuleType, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    checkout_path = project_path / ".lake/packages/mathlib"
    _write(checkout_path / "Mathlib/Provenance.lean", "theorem claim : True := by trivial\n")
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.name", "Autoform Test"),
        ("git", "config", "user.email", "autoform@example.invalid"),
        ("git", "add", "Mathlib/Provenance.lean"),
        ("git", "commit", "-q", "-m", "fixture"),
        ("git", "config", "filter.counterfeit.clean", "false"),
    ):
        result = _run(checkout_path, *command)
        assert result.returncode == 0, result.stdout + result.stderr
    revision = _run(checkout_path, "git", "rev-parse", "HEAD").stdout.strip()
    project = helper._open_project_tree(project_path)
    checkout = helper._open_mathlib_checkout(
        project,
        helper._MathlibManifest(PurePosixPath(".lake/packages"), revision),
    )
    try:
        with pytest.raises(helper.AuditInputError, match="unsafe local Git configuration"):
            helper._validate_mathlib_checkout(
                checkout,
                helper._MathlibManifest(PurePosixPath(".lake/packages"), revision),
                helper._Deadline.after(10),
                environment=os.environ,
            )
    finally:
        checkout.close()
        project.close()


def test_canonical_revision_fetch_is_shallow_filtered_and_hard_coded(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "a" * 40
    commands: list[tuple[str, ...]] = []

    def completed(arguments, **kwargs):
        command = tuple(str(value) for value in arguments)
        commands.append(command)
        if command[:3] == ("git", "init", "--bare"):
            Path(command[-1]).mkdir()
        stdout = (revision + "\n").encode() if "rev-parse" in command else b""
        return helper._CommandResult(command, 0, stdout, b"")

    monkeypatch.setattr(helper, "_checked_command", completed)

    helper._verify_canonical_mathlib_revision(
        tmp_path,
        revision,
        helper._Deadline.after(10),
        environment=os.environ,
    )

    fetch = next(command for command in commands if "fetch" in command)
    assert "--depth=1" in fetch
    assert "--filter=blob:none" in fetch
    assert "https://github.com/leanprover-community/mathlib4.git" in fetch
    assert revision in fetch

    wrong_private = tmp_path / "wrong"
    wrong_private.mkdir()

    def wrong_revision(arguments, **kwargs):
        command = tuple(str(value) for value in arguments)
        if command[:3] == ("git", "init", "--bare"):
            Path(command[-1]).mkdir()
        stdout = ("b" * 40 + "\n").encode() if "rev-parse" in command else b""
        return helper._CommandResult(command, 0, stdout, b"")

    monkeypatch.setattr(helper, "_checked_command", wrong_revision)
    with pytest.raises(helper.AuditInputError, match="did not resolve the manifest revision"):
        helper._verify_canonical_mathlib_revision(
            wrong_private,
            revision,
            helper._Deadline.after(10),
            environment=os.environ,
        )


def test_mathlib_query_is_batched_rehashed_and_package_qualified(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    checkout = project / ".lake/packages/mathlib"
    modules = ("Mathlib.Alpha", "Mathlib.Beta")
    outputs = []
    for module in modules:
        stem = checkout / ".lake/build/lib/lean" / Path(*module.split("."))
        outputs.extend((json.dumps(str(stem.with_suffix(".ilean"))), json.dumps(str(stem.with_suffix(".olean")))))
    commands: list[tuple[str, ...]] = []

    def completed(arguments, **kwargs):
        command = tuple(str(value) for value in arguments)
        commands.append(command)
        return helper._CommandResult(command, 0, ("\n".join(outputs) + "\n").encode(), b"")

    monkeypatch.setattr(helper, "_checked_command", completed)

    paths = helper._query_mathlib_artifact_paths(
        project,
        checkout,
        modules,
        helper._Deadline.after(10),
        environment=os.environ,
    )

    assert tuple(paths) == modules
    assert commands == [
        (
            "lake",
            "--rehash",
            "--json",
            "query",
            "@mathlib/+Mathlib.Alpha:ilean",
            "@mathlib/+Mathlib.Alpha:olean",
            "@mathlib/+Mathlib.Beta:ilean",
            "@mathlib/+Mathlib.Beta:olean",
        )
    ]

    outputs[0] = json.dumps(str(tmp_path / "outside/Mathlib/Alpha.ilean"))
    with pytest.raises(helper.AuditInputError, match="escapes its owning directory"):
        helper._query_mathlib_artifact_paths(
            project,
            checkout,
            modules,
            helper._Deadline.after(10),
            environment=os.environ,
        )


def test_mathlib_modules_must_share_one_query_derived_build_root(
    helper: ModuleType, tmp_path: Path
) -> None:
    checkout = tmp_path / "mathlib"
    paths = {
        "Mathlib.Alpha": (
            checkout / "first/lib/lean/Mathlib/Alpha.ilean",
            checkout / "first/lib/lean/Mathlib/Alpha.olean",
        ),
        "Mathlib.Beta": (
            checkout / "second/lib/lean/Mathlib/Beta.ilean",
            checkout / "second/lib/lean/Mathlib/Beta.olean",
        ),
    }

    with pytest.raises(helper.AuditInputError, match="multiple build roots"):
        helper._mathlib_build_roots(checkout, paths)


def test_mathlib_cached_trace_requires_exact_primary_content_descriptors(
    helper: ModuleType
) -> None:
    trace = {
        "schemaVersion": "2025-09-10",
        "depHash": "1" * 16,
        "outputs": {
            "i": "2" * 16 + ".ilean",
            "o": ["3" * 16 + ".olean", "4" * 16 + ".olean.server"],
        },
    }

    assert helper._mathlib_trace_hashes(trace, "Mathlib.Provenance", "trace") == (
        "2" * 16,
        "3" * 16,
    )

    trace["outputs"]["o"].append("5" * 16 + ".olean")
    with pytest.raises(helper.AuditInputError, match="trace outputs"):
        helper._mathlib_trace_hashes(trace, "Mathlib.Provenance", "trace")


def test_mathlib_full_trace_requires_module_and_package_ownership(helper: ModuleType) -> None:
    trace = {
        "synthetic": False,
        "schemaVersion": "2025-09-10",
        "depHash": "1" * 16,
        "outputs": {"i": "2" * 16 + ".ilean", "o": ["3" * 16 + ".olean"]},
        "inputs": [
            ["Module.name: Mathlib.Provenance", "hash"],
            ["Package.id?: (some mathlib)", "hash"],
        ],
    }
    assert helper._mathlib_trace_hashes(trace, "Mathlib.Provenance", "trace") == (
        "2" * 16,
        "3" * 16,
    )

    trace["inputs"][1][0] = "Package.id?: (some counterfeit)"
    with pytest.raises(helper.AuditInputError, match="package id 'mathlib'"):
        helper._mathlib_trace_hashes(trace, "Mathlib.Provenance", "trace")


def test_mathlib_artifacts_bind_ilean_declaration_trace_and_olean(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout_path = tmp_path / "mathlib"
    stem = checkout_path / ".lake/build/lib/lean/Mathlib/Provenance"
    ilean = stem.with_suffix(".ilean")
    olean = stem.with_suffix(".olean")
    _write(
        ilean,
        _metadata(
            "Mathlib.Provenance",
            declarations={"Mathlib.Provenance.claim": []},
        ).decode(),
    )
    _write(olean, "olean bytes")
    _write(
        stem.with_suffix(".trace"),
        json.dumps(
            {
                "schemaVersion": "2025-09-10",
                "depHash": "1" * 16,
                "outputs": {
                    "i": "2" * 16 + ".ilean",
                    "o": ["3" * 16 + ".olean"],
                },
            }
        ),
    )
    target = helper.BlueprintTarget(
        "roadmap/upstream.md",
        "Mathlib.Provenance.claim",
        "theorem",
        owner="mathlib",
        expected_module="Mathlib.Provenance",
        source_file="Mathlib/Provenance.lean",
    )

    def hashes(_project, _helper_path, paths, _deadline, **_kwargs):
        return {
            path: ("2" * 16 if path.suffix == ".ilean" else "3" * 16)
            for path in paths
        }

    monkeypatch.setattr(helper, "_lake_content_hashes", hashes)
    checkout = helper.BoundDirectoryTree(checkout_path)
    try:
        (tmp_path / "first").mkdir()
        evidence = helper._inspect_mathlib_artifacts(
            checkout,
            {"Mathlib.Provenance": (ilean, olean)},
            (target,),
            tmp_path / "first",
            tmp_path,
            helper._Deadline.after(10),
            environment=os.environ,
        )
        assert evidence.olean_paths == (("Mathlib.Provenance", str(olean)),)

        _write(ilean, _metadata("Mathlib.Provenance", declarations={}).decode())
        (tmp_path / "missing").mkdir()
        with pytest.raises(helper.AuditInputError, match="lacks claimed declaration"):
            helper._inspect_mathlib_artifacts(
                checkout,
                {"Mathlib.Provenance": (ilean, olean)},
                (target,),
                tmp_path / "missing",
                tmp_path,
                helper._Deadline.after(10),
                environment=os.environ,
            )

        _write(
            ilean,
            _metadata(
                "Mathlib.Provenance",
                declarations={"Mathlib.Provenance.claim": []},
            ).decode(),
        )

        def tamper(_project, _helper_path, paths, _deadline, **_kwargs):
            _write(olean, "changed after hashing")
            return hashes(_project, _helper_path, paths, _deadline, **_kwargs)

        monkeypatch.setattr(helper, "_lake_content_hashes", tamper)
        (tmp_path / "tampered").mkdir()
        with pytest.raises(helper.AuditInputError, match="does not match expected bytes"):
            helper._inspect_mathlib_artifacts(
                checkout,
                {"Mathlib.Provenance": (ilean, olean)},
                (target,),
                tmp_path / "tampered",
                tmp_path,
                helper._Deadline.after(10),
                environment=os.environ,
            )
    finally:
        checkout.close()


def test_probe_requires_and_embeds_exact_mathlib_olean_path(helper: ModuleType) -> None:
    target = helper.BlueprintTarget(
        "roadmap/upstream.md",
        "Nat.prime_def_lt",
        "theorem",
        owner="mathlib",
        expected_module="Mathlib.Data.Nat.Prime.Defs",
        source_file="Mathlib/Data/Nat/Prime/Defs.lean",
    )
    with pytest.raises(helper.AuditInputError, match="lack validated build artifacts"):
        helper.render_probe(("Fixture",), (target,))

    probe = helper.render_probe(
        ("Fixture",),
        (target,),
        {
            "Mathlib.Data.Nat.Prime.Defs": (
                "/checkout/.lake/build/lib/lean/Mathlib/Data/Nat/Prime/Defs.olean"
            )
        },
    )
    assert "import Mathlib.Data.Nat.Prime.Defs" in probe
    assert "Lean.findOLean moduleName" in probe
    assert "/checkout/.lake/build/lib/lean/Mathlib/Data/Nat/Prime/Defs.olean" in probe
    assert "belongs to {moduleName}, not {expectedModule}" in probe


def test_archive_rejects_aliases_special_members_and_named_limits(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = _archive(
        tmp_path / "alias.tgz",
        [
            ("./lib/lean/Fixture.ilean", _metadata("Fixture")),
            ("./lib/LEAN/Fixture.ilean", _metadata("Fixture")),
        ],
    )
    with pytest.raises(helper.AuditInputError, match="aliased"):
        helper.modules_from_archive(alias, "Fixture")

    special = _archive(tmp_path / "special.tgz", [("./lib/lean/Fixture.ilean", None)])
    with pytest.raises(helper.AuditInputError, match="not a regular file"):
        helper.modules_from_archive(special, "Fixture")

    monkeypatch.setattr(helper, "_MAX_ARCHIVE_MEMBERS", 2)
    bounded = _archive(tmp_path / "bounded.tgz", _module_members("Fixture"))
    with pytest.raises(helper.AuditInputError, match="exceeds 2 members"):
        helper.modules_from_archive(bounded, "Fixture")


def test_config_and_artifact_payload_limits_fail_before_parsing(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "evaluated.toml"
    _write(config, 'name = "Fixture"\n')
    monkeypatch.setattr(helper, "_MAX_CONFIG_BYTES", 4)
    with pytest.raises(helper.AuditInputError, match="exceeds 4 bytes"):
        helper.root_package_from_config(config)

    monkeypatch.setattr(helper, "_MAX_ILEAN_BYTES", 4)
    archive = _archive(tmp_path / "oversized.tgz", _module_members("Fixture"))
    with pytest.raises(helper.AuditInputError, match="ILean archive member exceeds 4 bytes"):
        helper.modules_from_archive(archive, "Fixture")


def test_archive_aggregate_content_and_name_bytes_are_bounded(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path / "content.tgz", _module_members("Fixture"))
    monkeypatch.setattr(helper, "_MAX_ARCHIVE_CONTENT_BYTES", 8)
    with pytest.raises(helper.AuditInputError, match="aggregate decompressed byte limit 8"):
        helper.modules_from_archive(archive, "Fixture")

    monkeypatch.setattr(helper, "_MAX_ARCHIVE_CONTENT_BYTES", 1024 * 1024)
    monkeypatch.setattr(helper, "_MAX_ARCHIVE_NAME_BYTES", 8)
    with pytest.raises(helper.AuditInputError, match="aggregate member-name byte limit 8"):
        helper.modules_from_archive(archive, "Fixture")


@pytest.mark.parametrize("suffix", [".olean", ".trace"])
def test_archive_rejects_orphan_module_artifact_extensions(
    helper: ModuleType, tmp_path: Path, suffix: str
) -> None:
    archive = _archive(
        tmp_path / "orphan.tgz",
        [*_module_members("Fixture"), (f"./lib/lean/Orphan{suffix}", b"orphan")],
    )

    with pytest.raises(helper.AuditInputError, match="contains orphan artifact"):
        helper.modules_from_archive(archive, "Fixture")


def test_archive_allows_nonmodule_build_traces(
    helper: ModuleType, tmp_path: Path
) -> None:
    archive = _archive(
        tmp_path / "executable.tgz",
        [*_module_members("Fixture"), ("./bin/runner.trace", b"not a module trace")],
    )

    assert helper.modules_from_archive(archive, "Fixture") == ("Fixture",)


def test_artifact_json_rejects_duplicate_keys(helper: ModuleType, tmp_path: Path) -> None:
    duplicate = (
        b'{"module":"Fixture","module":"Counterfeit","version":5,'
        b'"decls":{},"references":{},"directImports":[]}'
    )
    archive = _archive(
        tmp_path / "duplicate-json.tgz",
        [
            ("./lib/lean/Fixture.ilean", duplicate),
            ("./lib/lean/Fixture.olean", b"olean"),
            ("./lib/lean/Fixture.trace", _trace("Fixture")),
        ],
    )

    with pytest.raises(helper.AuditInputError, match="duplicate JSON key 'module'"):
        helper.modules_from_archive(archive, "Fixture")


def test_project_input_snapshot_requires_controls_and_strict_manifest(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(project / "lakefile.toml", 'name = "Fixture"\n')
    _write(project / "lake-manifest.json", '{"version":"1","version":"2"}\n')
    _write(project / "Fixture.lean", "theorem Fixture.claim : True := by trivial\n")
    tree = helper._open_project_tree(project)
    try:
        with pytest.raises(helper.AuditInputError, match="duplicate JSON key 'version'"):
            helper._capture_project_inputs(tree)
    finally:
        tree.close()


@pytest.mark.parametrize(
    ("filename", "limit_name"),
    [
        ("lean-toolchain", "_MAX_TOOLCHAIN_BYTES"),
        ("lakefile.toml", "_MAX_CONFIG_BYTES"),
        ("lake-manifest.json", "_MAX_MANIFEST_BYTES"),
    ],
)
def test_project_control_files_reject_tree_selection_sentinel_bytes(
    helper: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    limit_name: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "lean-toolchain", "lean\n")
    _write(project / "lakefile.toml", "name\n")
    _write(project / "lake-manifest.json", "{}\n")
    _write(project / filename, "xxxxx")
    monkeypatch.setattr(helper, limit_name, 4)
    tree = helper._open_project_tree(project)
    try:
        with pytest.raises(helper.AuditInputError, match=rf"{filename} exceeds 4 bytes"):
            helper._capture_project_inputs(tree)
    finally:
        tree.close()


def test_project_input_snapshot_rejects_case_aliases(
    helper: ModuleType, tmp_path: Path
) -> None:
    del tmp_path
    with pytest.raises(helper.AuditInputError, match="case- or Unicode-aliased"):
        helper._reject_file_aliases(("Fixture.lean", "fixture.lean"))


def test_project_input_snapshot_rejects_symlinked_controls(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "actual-toolchain", "leanprover/lean4:v4.32.2\n")
    (project / "lean-toolchain").symlink_to("actual-toolchain")
    _write(project / "lakefile.toml", 'name = "Fixture"\n')
    _write(project / "lake-manifest.json", "{}\n")
    tree = helper._open_project_tree(project)
    try:
        with pytest.raises(helper.AuditInputError, match="symbolic links are not supported"):
            helper._capture_project_inputs(tree)
    finally:
        tree.close()


def test_root_source_and_artifact_paths_cannot_escape_project(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    trace = {
        "synthetic": False,
        "inputs": [["../Fixture.lean", "hash"]],
    }
    with pytest.raises(helper.AuditInputError, match="not canonical"):
        helper._root_source_from_trace(trace, project, "Fixture")
    with pytest.raises(helper.AuditInputError, match="escapes"):
        helper._validate_live_artifact_path(
            project,
            project / "../outside/lib/lean/Fixture.ilean",
            "Fixture",
        )


def test_artifact_snapshot_rejects_symlinked_files(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    artifact = project / ".lake/build/lib/lean/Fixture.ilean"
    artifact.parent.mkdir(parents=True)
    outside = tmp_path / "outside.ilean"
    outside.write_bytes(b"artifact")
    artifact.symlink_to(outside)
    tree = helper._open_project_tree(project)
    try:
        with pytest.raises(helper.AuditInputError, match="unsafe root-package artifact"):
            helper._capture_artifact_set(
                tree,
                (".lake/build/lib/lean/Fixture.ilean",),
                expected_digests={
                    ".lake/build/lib/lean/Fixture.ilean": hashlib.sha256(b"artifact").hexdigest()
                },
            )
    finally:
        tree.close()


def test_archive_mutation_during_parse_is_rejected(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path / "root.tgz", _module_members("Fixture"))
    original = helper._validate_root_trace
    mutated = False

    def mutate(trace, module, package, display):
        nonlocal mutated
        original(trace, module, package, display)
        if not mutated:
            mutated = True
            with archive.open("ab") as stream:
                stream.write(b"mutated")

    monkeypatch.setattr(helper, "_validate_root_trace", mutate)

    with pytest.raises(helper.AuditInputError, match="archive changed"):
        helper.modules_from_archive(archive, "Fixture")


def test_subprocess_environment_output_and_deadline_are_bounded(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = {
        "PATH": os.environ["PATH"],
        "SAFE_VALUE": "kept",
        "ELAN_TOOLCHAIN": "counterfeit",
        "GIT_DIR": "/counterfeit",
        "LAKE_OPTS": "--counterfeit",
        "LEAN_OPTS": "--counterfeit",
        "LEAN_PATH": "/counterfeit",
        "PYTHONPATH": "/counterfeit",
    }
    script = (
        "import json, os; "
        "print(json.dumps({key: os.environ.get(key) for key in "
        "['SAFE_VALUE', 'ELAN_TOOLCHAIN', 'GIT_DIR', 'LAKE_OPTS', "
        "'LEAN_OPTS', 'LEAN_PATH', 'PYTHONPATH']}))"
    )
    result = helper._run_bounded_command(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        deadline=helper._Deadline.after(5),
        environment=helper._audit_environment(environment),
    )
    assert json.loads(result.stdout) == {
        "SAFE_VALUE": "kept",
        "ELAN_TOOLCHAIN": None,
        "GIT_DIR": None,
        "LAKE_OPTS": None,
        "LEAN_OPTS": None,
        "LEAN_PATH": None,
        "PYTHONPATH": None,
    }

    monkeypatch.setattr(helper, "_MAX_OUTPUT_BYTES", 32)
    with pytest.raises(helper.AuditInputError, match="output exceeds"):
        helper._run_bounded_command(
            [sys.executable, "-c", "print('x' * 64)"],
            cwd=tmp_path,
            deadline=helper._Deadline.after(5),
            environment=helper._audit_environment(environment),
        )
    with pytest.raises(helper.AuditInputError, match="aggregate subprocess deadline"):
        helper._run_bounded_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            deadline=helper._Deadline.after(0.05),
            environment=helper._audit_environment(environment),
        )


@pytest.mark.parametrize("seconds", [0, -1, True, float("inf"), float("nan")])
def test_subprocess_deadline_must_be_finite_and_positive(
    helper: ModuleType, seconds: object
) -> None:
    with pytest.raises(helper.AuditInputError, match="deadline must be positive"):
        helper._Deadline.after(seconds)


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
def test_subprocess_runner_cleans_up_surviving_descendants(
    helper: ModuleType, tmp_path: Path
) -> None:
    script = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print(child.pid, flush=True)"
    )
    started = time.monotonic()
    result = helper._run_bounded_command(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        deadline=helper._Deadline.after(5),
        environment=helper._audit_environment(os.environ),
    )

    assert result.returncode == 0
    assert time.monotonic() - started < 2
    child_pid = int(result.stdout)
    for _attempt in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("artifact subprocess descendant survived process-group cleanup")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("config", "evaluated Lake configuration changed"),
        ("archive", "root-package build archive changed"),
        ("probe", "generated artifact audit probe changed"),
        ("blueprint", "blueprint changed"),
        ("toolchain", "Lean project inputs changed"),
        ("lakefile", "Lean project inputs changed"),
        ("manifest", "Lake manifest"),
        ("source", "Lean project inputs changed"),
        ("ilean", "live root-package artifact does not match packed bytes"),
        ("olean", "live root-package artifact does not match packed bytes"),
        ("trace", "live root-package artifact does not match packed bytes"),
    ],
)
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_artifact_gate_revalidates_every_local_evidence_boundary_after_lean(
    helper: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    project, blueprint = _built_local_audit_fixture(tmp_path)
    targets = {
        "blueprint": blueprint / "roadmap/claim.md",
        "toolchain": project / "lean-toolchain",
        "lakefile": project / "lakefile.toml",
        "manifest": project / "lake-manifest.json",
        "source": project / "Fixture.lean",
        "ilean": project / ".lake/build/lib/lean/Fixture.ilean",
        "olean": project / ".lake/build/lib/lean/Fixture.olean",
        "trace": project / ".lake/build/lib/lean/Fixture.trace",
    }
    original_command = helper._checked_command
    original_open = helper._open_bound_file
    private_evidence: dict[str, Path] = {}

    def capture_private(path, label, maximum_bytes, *, collect, **kwargs):
        evidence = original_open(path, label, maximum_bytes, collect=collect, **kwargs)
        private_evidence[label] = evidence.path
        return evidence

    def mutate_after_probe(arguments, **kwargs):
        result = original_command(arguments, **kwargs)
        if kwargs.get("label") == "Lean artifact probe":
            path = {
                "config": private_evidence["evaluated Lake configuration"],
                "archive": private_evidence["root-package build archive"],
                "probe": private_evidence["generated artifact audit probe"],
            }.get(mutation, targets.get(mutation))
            assert path is not None
            with path.open("ab") as stream:
                stream.write(b"\nmutated\n")
        return result

    monkeypatch.setattr(helper, "_open_bound_file", capture_private)
    monkeypatch.setattr(helper, "_checked_command", mutate_after_probe)

    with pytest.raises(helper.AuditInputError, match=message):
        helper.run_artifact_audit(blueprint, project)


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
            "outside lib/lean",
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
    helper_path = repo_root / _TEMPLATE
    config = tmp_path / "evaluated.toml"
    _write(config, 'name = "Fixture"\n')
    identified = subprocess.run(
        [sys.executable, "-I", str(helper_path), "--root-package", str(config)],
        capture_output=True,
        text=True,
    )
    assert identified.returncode == 0, identified.stderr
    assert identified.stdout == "Fixture\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(project: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=180)


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

    blueprint = _blueprint(project)
    _article(blueprint, "helper", "declaration: theorem", "lean: helper_ok")
    config = project / "evaluated.toml"
    translated = _run(project, "lake", "translate-config", "toml", str(config))
    assert translated.returncode == 0, translated.stdout + translated.stderr
    summary = helper.run_artifact_audit(blueprint, project)
    assert summary.root_modules == ("Chosen.Entry", "Chosen.Helper", "Main")


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_lake_hash_and_kernel_probe_bind_exact_olean(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "probe-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.toml",
        'name = "ProbeFixture"\nversion = "0.1.0"\n'
        'defaultTargets = ["Dependency", "Root"]\n\n'
        '[[lean_lib]]\nname = "Dependency"\n\n'
        '[[lean_lib]]\nname = "Root"\n',
    )
    _write(project / "Dependency.lean", "theorem Dependency.claim : True := by trivial\n")
    _write(
        project / "Root.lean",
        "theorem Root.claim : True := by trivial\n",
    )
    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    olean = project / ".lake/build/lib/lean/Dependency.olean"

    hash_helper = tmp_path / "lake-content-hash.lean"
    hash_helper.write_bytes(helper._lake_hash_helper_source())
    first = helper._lake_content_hashes(
        project,
        hash_helper,
        (olean,),
        helper._Deadline.after(30),
        environment=os.environ,
    )
    assert len(first[olean]) == 16
    _write(olean.with_suffix(".olean.hash"), "0" * 16)
    second = helper._lake_content_hashes(
        project,
        hash_helper,
        (olean,),
        helper._Deadline.after(30),
        environment=os.environ,
    )
    assert second == first

    target = helper.BlueprintTarget(
        "roadmap/dependency.md",
        "Dependency.claim",
        "theorem",
        owner="mathlib",
        expected_module="Dependency",
        source_file="Mathlib/Dependency.lean",
    )
    probe = project / "probe.lean"
    probe.write_text(
        helper.render_probe(("Root",), (target,), {"Dependency": str(olean)}),
        encoding="utf-8",
    )
    audited = _run(project, "lake", "env", "lean", "--trust=0", str(probe))
    assert audited.returncode == 0, audited.stdout + audited.stderr

    wrong_probe = project / "wrong-probe.lean"
    wrong_probe.write_text(
        helper.render_probe(
            ("Root",),
            (target,),
            {"Dependency": str(project / ".lake/build/lib/lean/Root.olean")},
        ),
        encoding="utf-8",
    )
    rejected = _run(project, "lake", "env", "lean", "--trust=0", str(wrong_probe))
    assert rejected.returncode != 0
    assert "not validated artifact" in rejected.stdout + rejected.stderr


@pytest.mark.parametrize("mutation", [None, "source", "artifact"])
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_gate_builds_and_verifies_claimed_mathlib_module_not_imported_by_root(
    helper: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str | None,
) -> None:
    project = tmp_path / "project"
    checkout = project / ".lake/packages/mathlib"
    checkout.mkdir(parents=True)
    _write(checkout / ".gitignore", "/.lake/\n/custom-output/\n")
    _write(
        checkout / "lakefile.lean",
        '''import Lake
open Lake DSL
package mathlib where
  buildDir := "custom-output"
@[default_target]
lean_lib Mathlib
''',
    )
    _write(
        checkout / "Mathlib/Provenance.lean",
        "theorem Mathlib.Provenance.claim : True := by trivial\n",
    )
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.name", "Autoform Test"),
        ("git", "config", "user.email", "autoform@example.invalid"),
        ("git", "add", ".gitignore", "lakefile.lean", "Mathlib/Provenance.lean"),
        ("git", "commit", "-q", "-m", "fixture"),
    ):
        result = _run(checkout, *command)
        assert result.returncode == 0, result.stdout + result.stderr
    revision = _run(checkout, "git", "rev-parse", "HEAD").stdout.strip()

    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.toml",
        'name = "Fixture"\nversion = "0.1.0"\ndefaultTargets = ["Root"]\n\n'
        '[[lean_lib]]\nname = "Root"\n\n'
        '[[require]]\nname = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        f'rev = "{revision}"\n',
    )
    _write(project / "Root.lean", "theorem Root.claim : True := by trivial\n")
    manifest = {
        "version": "1.2.0",
        "packagesDir": ".lake/packages",
        "packages": [
            {
                "url": "https://github.com/leanprover-community/mathlib4.git",
                "type": "git",
                "subDir": None,
                "scope": "",
                "rev": revision,
                "name": "mathlib",
                "manifestFile": "lake-manifest.json",
                "inputRev": revision,
                "inherited": False,
                "configFile": "lakefile.lean",
            }
        ],
        "name": "Fixture",
        "lakeDir": ".lake",
    }
    _write(project / "lake-manifest.json", json.dumps(manifest, sort_keys=True) + "\n")
    blueprint = _blueprint(project)
    _article(blueprint, "root", "declaration: theorem", "lean: Root.claim")
    _article(
        blueprint,
        "upstream",
        "declaration: theorem",
        "mathlib: true",
        "mathlib_declaration: Mathlib.Provenance.claim",
        "mathlib_file: Mathlib/Provenance.lean",
    )
    claimed_olean = checkout / "custom-output/lib/lean/Mathlib/Provenance.olean"
    assert "Mathlib" not in (project / "Root.lean").read_text(encoding="utf-8")
    assert not claimed_olean.exists()
    monkeypatch.setattr(helper, "_verify_canonical_mathlib_revision", lambda *_args, **_kwargs: None)
    original_command = helper._checked_command

    def mutate_after_probe(arguments, **kwargs):
        result = original_command(arguments, **kwargs)
        if kwargs.get("label") == "Lean artifact probe":
            if mutation == "source":
                _write(
                    checkout / "Mathlib/Provenance.lean",
                    "theorem Mathlib.Provenance.changed : True := by trivial\n",
                )
            elif mutation == "artifact":
                with claimed_olean.open("ab") as stream:
                    stream.write(b"changed")
        return result

    monkeypatch.setattr(helper, "_checked_command", mutate_after_probe)

    if mutation is not None:
        with pytest.raises(
            helper.AuditInputError,
            match="tracked or visible untracked changes|does not match expected bytes",
        ):
            helper.run_artifact_audit(blueprint, project)
        return

    summary = helper.run_artifact_audit(blueprint, project)

    assert summary.root_modules == ("Root",)
    assert summary.mathlib_modules == ("Mathlib.Provenance",)
    assert summary.target_count == 2
    assert claimed_olean.is_file()


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
    blueprint = _blueprint(project)
    _article(blueprint, "fresh", "declaration: theorem", "lean: fresh_ok")

    summary = helper.run_artifact_audit(blueprint, project)

    assert summary.root_modules == ("Fresh",)
    assert not (stale / "Stale.ilean").exists()


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

    blueprint = _blueprint(project)
    _article(blueprint, "internal", "declaration: theorem", "lean: internal_ok")
    summary = helper.run_artifact_audit(blueprint, project)
    assert summary.root_modules == ("PublicApi.Entry", "PublicApi.Internal")


def test_example_and_template_helpers_are_identical(repo_root: Path) -> None:
    template = repo_root / _TEMPLATE
    example = repo_root / "skills/setup/assets/cabannes-thesis-project/.github/autoform_audit.py"

    assert example.read_bytes() == template.read_bytes()


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_artifact_gate_accepts_quoted_package_name_as_one_argument(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "quoted-package"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.lean",
        '''import Lake
open Lake DSL
package «formal-math»
@[default_target]
lean_lib «FormalMath»
''',
    )
    _write(project / "FormalMath.lean", "theorem FormalMath.claim : True := by trivial\n")
    initialized = _run(project, "lake", "env", "true")
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    blueprint = _blueprint(project)
    _article(blueprint, "claim", "declaration: theorem", "lean: FormalMath.claim")
    commands: list[tuple[str, ...]] = []
    original = helper._checked_command

    def record(arguments, **kwargs):
        commands.append(tuple(str(argument) for argument in arguments))
        return original(arguments, **kwargs)

    monkeypatch.setattr(helper, "_checked_command", record)

    summary = helper.run_artifact_audit(blueprint, project)

    assert summary.root_package == "«formal-math»"
    assert ("lake", "clean", "«formal-math»") in commands
    assert any("@«formal-math»/+FormalMath:ilean" in command for command in commands)


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_artifact_gate_checks_all_supported_local_declaration_kinds(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "verified-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.toml",
        'name = "Fixture"\nversion = "0.1.0"\ndefaultTargets = ["Fixture"]\n\n'
        '[[lean_lib]]\nname = "Fixture"\n',
    )
    _write(
        project / "Fixture.lean",
        """theorem Fixture.theoremClaim : True := by trivial
def Fixture.definitionClaim : Nat := 1
abbrev Fixture.abbreviationClaim : Nat := 1
instance Fixture.instanceClaim : Inhabited (Fin 1) := inferInstance
structure Fixture.StructureClaim where value : Nat
class Fixture.ClassClaim where value : Nat
inductive Fixture.InductiveClaim where | value
opaque Fixture.opaqueClaim : Nat := 1
""",
    )
    blueprint = _blueprint(project)
    claims = (
        ("theorem", "theorem", "Fixture.theoremClaim"),
        ("definition", "definition", "Fixture.definitionClaim"),
        ("abbreviation", "abbrev", "Fixture.abbreviationClaim"),
        ("instance", "instance", "Fixture.instanceClaim"),
        ("structure", "structure", "Fixture.StructureClaim"),
        ("class", "class", "Fixture.ClassClaim"),
        ("inductive", "inductive", "Fixture.InductiveClaim"),
        ("opaque", "opaque", "Fixture.opaqueClaim"),
    )
    for article, kind, declaration in claims:
        _article(blueprint, article, f"declaration: {kind}", f"lean: {declaration}")

    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    config = project / "evaluated.toml"
    translated = _run(project, "lake", "translate-config", "toml", str(config))
    assert translated.returncode == 0, translated.stdout + translated.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr

    labels: list[str] = []
    original = helper._checked_command

    def record(arguments, **kwargs):
        labels.append(kwargs["label"])
        if kwargs["label"] == "Lean artifact probe":
            assert list(arguments)[-2] == "--trust=0"
        return original(arguments, **kwargs)

    monkeypatch.setattr(helper, "_checked_command", record)

    summary = helper.run_artifact_audit(blueprint, project)

    assert summary.root_package == "Fixture"
    assert summary.root_modules == ("Fixture",)
    assert summary.target_count == len(claims)
    assert {
        "evaluated Lake configuration",
        "Lake build-directory check",
        "root-package clean",
        "root-package build",
        "root-package archive",
        "Lean artifact probe",
    }.issubset(labels)


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_artifact_gate_rejects_untrusted_and_mismatched_declarations(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "rejected-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.toml",
        'name = "Rejected"\nversion = "0.1.0"\ndefaultTargets = ["Rejected"]\n\n'
        '[[lean_lib]]\nname = "Rejected"\n',
    )
    _write(
        project / "Rejected.lean",
        """def Rejected.value : Nat := 1
axiom Rejected.assumed : True
theorem Rejected.admitted : True := by sorry
unsafe def Rejected.unsafeValue : Nat := 1
partial def Rejected.partialValue (n : Nat) : Nat := Rejected.partialValue n
""",
    )
    blueprint = _blueprint(project)
    _article(blueprint, "wrong-kind", "declaration: theorem", "lean: Rejected.value")
    _article(blueprint, "wrong-owner", "declaration: def", "lean: Nat.add")
    _article(blueprint, "missing", "declaration: theorem", "lean: Rejected.missing")
    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    config = project / "evaluated.toml"
    assert _run(project, "lake", "translate-config", "toml", str(config)).returncode == 0
    archive = project / "root.tgz"
    assert _run(project, "lake", "pack", str(archive)).returncode == 0

    with pytest.raises(helper.AuditInputError, match="Lean artifact probe failed"):
        helper.run_artifact_audit(blueprint, project)


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_artifact_gate_rejects_a_root_module_with_no_declarations(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "empty-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
    _write(
        project / "lakefile.toml",
        'name = "Empty"\nversion = "0.1.0"\ndefaultTargets = ["Empty"]\n\n'
        '[[lean_lib]]\nname = "Empty"\n',
    )
    _write(project / "Empty.lean", "import Init\n")
    blueprint = _blueprint(project)
    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    config = project / "evaluated.toml"
    assert _run(project, "lake", "translate-config", "toml", str(config)).returncode == 0
    archive = project / "root.tgz"
    assert _run(project, "lake", "pack", str(archive)).returncode == 0

    with pytest.raises(helper.AuditInputError, match="Lean artifact probe failed"):
        helper.run_artifact_audit(blueprint, project)
