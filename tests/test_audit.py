from __future__ import annotations

import json
from pathlib import Path

from autoform_cli.audit import audit_blueprint


def _article(
    blueprint: Path,
    relative: str,
    prose: str = "A precise mathematical statement.",
    *,
    depends: bool = True,
    sources: tuple[str, ...] = (),
    **metadata: str,
) -> Path:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    title = path.stem.replace("-", " ").title()
    properties = [*(f"{key}: {value}" for key, value in metadata.items())]
    lines = ["---", *properties, "---", "", f"# {title}", "", prose]
    if sources:
        lines.extend(["", "## Sources", "", *(f"- [source]({target})" for target in sources)])
    if depends:
        lines.extend(["", "## Depends on", "", "This article has no prerequisites."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _coverage(blueprint: Path, text: str = "The roadmap covers every declared target.") -> Path:
    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Coverage\n\n{text}\n", encoding="utf-8")
    return path


def _finding_map(blueprint: Path, *, lean_root: Path | None = None) -> dict[str, list[tuple[str, str]]]:
    result = audit_blueprint(blueprint, lean_root=lean_root)
    findings: dict[str, list[tuple[str, str]]] = {}
    for finding in result.findings:
        findings.setdefault(finding.article_path, []).append((finding.code, finding.reason))
    return findings


def test_clean_audit_has_stable_machine_readable_representation(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "result.md",
        declaration="theorem",
        statement="formalized",
        proof="formalized",
        lean="Project.result",
    )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Result.lean").write_text("theorem Project.result : True := trivial\n", encoding="utf-8")

    first = audit_blueprint(blueprint, lean_root=lean_root)
    second = audit_blueprint(blueprint, lean_root=lean_root)

    assert first.clean
    assert first.findings == ()
    assert first.as_dict() == {"clean": True, "findings": []}
    assert first.to_json() == '{"clean":true,"findings":[]}'
    assert second.to_json() == first.to_json()
    assert str(tmp_path) not in first.to_json()


def test_audit_reports_formalizable_structure_and_inconsistent_checked_facts(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "chapter/README.md",
        prose="",
        depends=False,
        declaration="theorem",
        proof="formalized",
    )
    _article(blueprint, "chapter/child.md", declaration="lemma")

    findings = _finding_map(blueprint)["roadmap/chapter/README.md"]
    codes = {code for code, _reason in findings}

    assert codes == {
        "formalizable-container",
        "missing-depends-section",
        "missing-statement-text",
        "proof-without-statement",
    }
    assert all(reason for _code, reason in findings)


def test_audit_requires_mathlib_declaration_and_declaration_intent_on_evidenced_leaf(
    tmp_path: Path,
) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "upstream.md", mathlib="true")
    _article(blueprint, "local.md", statement="formalized", lean="Project.local")
    _article(blueprint, "exposition.md")

    findings = _finding_map(blueprint)

    upstream_codes = {code for code, _reason in findings["roadmap/upstream.md"]}
    local_codes = {code for code, _reason in findings["roadmap/local.md"]}
    assert upstream_codes == {"mathlib-without-declaration", "missing-declaration-intent"}
    assert local_codes == {"missing-declaration-intent"}
    assert "roadmap/exposition.md" not in findings


def test_audit_validates_local_source_links_without_network_access(tmp_path: Path, monkeypatch) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    source = blueprint / "sources" / "paper.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Paper\n\n## Theorem\n", encoding="utf-8")
    _article(
        blueprint,
        "chapter/result.md",
        declaration="theorem",
        origin="cited",
        sources=(
            "../../sources/paper.md#theorem",
            "../../sources/missing.md",
            "../../../outside.md",
            "https://example.invalid/paper",
            "ftp://example.invalid/paper",
            "//example.invalid/paper",
            "%00",
        ),
    )

    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("audit attempted network access")

    monkeypatch.setattr("socket.create_connection", fail_network)
    findings = _finding_map(blueprint)["roadmap/chapter/result.md"]

    assert findings == [
        ("malformed-source-link", "source link contains an invalid path: '%00'"),
        ("source-escapes-blueprint", "source link escapes the blueprint: '../../../outside.md'"),
        ("source-not-found", "source link does not resolve to a file: '../../sources/missing.md'"),
        ("unsupported-source-link", "source link uses a network location: '//example.invalid/paper'"),
        ("unsupported-source-link", "source link uses unsupported scheme: 'ftp://example.invalid/paper'"),
    ]


def test_audit_rejects_missing_markdown_source_anchor(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    source = blueprint / "sources" / "paper.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Paper\n\n## Actual theorem\n", encoding="utf-8")
    _article(
        blueprint,
        "result.md",
        declaration="theorem",
        origin="cited",
        sources=("../sources/paper.md#missing-theorem",),
    )

    findings = _finding_map(blueprint)["roadmap/result.md"]
    assert findings == [
        (
            "source-anchor-not-found",
            "source link fragment does not resolve: '../sources/paper.md#missing-theorem'",
        )
    ]


def test_audit_validates_lean_targets_only_when_root_is_supplied(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "missing.md",
        declaration="theorem",
        statement="formalized",
        lean="Project.missing",
    )
    _article(
        blueprint,
        "untargeted.md",
        declaration="lemma",
        statement="formalized",
    )
    _article(
        blueprint,
        "wrong-kind.md",
        declaration="theorem",
        statement="formalized",
        lean="Project.value",
    )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Value.lean").write_text("def Project.value : Nat := 1\n", encoding="utf-8")

    without_lean = _finding_map(blueprint)
    with_lean = _finding_map(blueprint, lean_root=lean_root)

    assert "roadmap/missing.md" not in without_lean
    assert with_lean["roadmap/missing.md"] == [
        ("lean-target-not-found", "Lean declaration target was not found: Project.missing")
    ]
    assert with_lean["roadmap/untargeted.md"] == [
        ("missing-lean-target", "formalized local work has no lean declaration target")
    ]
    assert with_lean["roadmap/wrong-kind.md"] == [
        ("lean-target-kind-mismatch", "Lean target kind def does not match declaration intent theorem")
    ]


def test_audit_reports_invalid_lean_root_once(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(
        blueprint,
        "result.md",
        declaration="theorem",
        statement="formalized",
        lean="Project.result",
    )

    result = audit_blueprint(blueprint, lean_root=tmp_path / "absent")

    assert [(finding.article_path, finding.code) for finding in result.findings] == [
        (".", "invalid-lean-root")
    ]


def test_audit_reports_coverage_gaps_provable_from_files(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "result.md", declaration="theorem")

    missing = audit_blueprint(blueprint)
    assert [(finding.article_path, finding.code) for finding in missing.findings] == [
        ("coverage/README.md", "missing-coverage-contract")
    ]

    _coverage(
        blueprint,
        "| Area | Coverage |\n| --- | --- |\n| Completed result | MAPPED |\n| Main result | PARTIAL |\n\n- TODO: map the corollaries\n\n[Missing](../roadmap/nope.md)\n\n[Remote](//example.invalid/coverage)",
    )
    findings = _finding_map(blueprint)["coverage/README.md"]

    assert findings == [
        ("coverage-not-found", "coverage link does not resolve to a file: '../roadmap/nope.md' (line 10)"),
        ("declared-coverage-gap", "coverage contract declares PARTIAL at line 6"),
        ("declared-coverage-gap", "coverage contract declares TODO at line 8"),
        (
            "unsupported-coverage-link",
            "coverage link uses a network location: '//example.invalid/coverage' (line 12)",
        ),
    ]


def test_unreadable_coverage_reason_is_stable(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "result.md", declaration="theorem")
    coverage = _coverage(blueprint)
    coverage.write_bytes(b"\xff")

    result = audit_blueprint(blueprint)

    finding = next(item for item in result.findings if item.code == "unreadable-coverage-file")
    assert finding.reason == "coverage file cannot be read as UTF-8"
    assert str(tmp_path) not in result.to_json()


def test_invalid_blueprint_result_does_not_leak_host_path(tmp_path: Path) -> None:
    result = audit_blueprint(tmp_path / "absent")

    assert not result.clean
    assert str(tmp_path) not in result.to_json()
    assert len(result.findings) == 1
    assert result.findings[0].article_path == "."
    assert result.findings[0].reason == "blueprint directory does not exist: ."


def test_audit_returns_graph_validation_errors_with_article_paths(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "bad.md", declaration="theorem")
    path = blueprint / "roadmap" / "bad.md"
    path.write_text("---\n---\nNo H1\n", encoding="utf-8")

    result = audit_blueprint(blueprint)

    assert not result.clean
    assert result.findings[0].article_path == "roadmap/bad.md"
    assert result.findings[0].code == "invalid-graph"
    assert result.findings[0].reason == "bad: missing H1 title"
    assert json.loads(result.to_json()) == result.as_dict()


def test_audit_is_read_only(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _coverage(blueprint)
    _article(blueprint, "result.md", declaration="theorem")
    before = {
        path.relative_to(blueprint).as_posix(): path.read_bytes()
        for path in sorted(blueprint.rglob("*"))
        if path.is_file()
    }

    audit_blueprint(blueprint)

    after = {
        path.relative_to(blueprint).as_posix(): path.read_bytes()
        for path in sorted(blueprint.rglob("*"))
        if path.is_file()
    }
    assert after == before
