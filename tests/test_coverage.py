from __future__ import annotations

import hashlib
from pathlib import Path

from autoform_cli.coverage import COVERAGE_SCHEMA, load_coverage


def _article(blueprint: Path, relative: str) -> None:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Roadmap article\n", encoding="utf-8")


def _contract(blueprint: Path, rows: str) -> Path:
    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        f"{rows}",
        encoding="utf-8",
    )
    return path


def test_loads_canonical_coverage_summary_with_stable_json(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    path = _contract(
        blueprint,
        "| Main theorem | `DECOMPOSED` | [Nodes](../roadmap/main/README.md) |\n"
        "| Corollaries | MAPPED | Source audit pending |\n"
        "| Experiments | OUT | Narrative only |\n"
        "| Appendix | DEFERRED | Revisit after milestone one |\n",
    )

    first, issues = load_coverage(blueprint)
    second, repeated_issues = load_coverage(blueprint)

    assert issues == repeated_issues == ()
    assert first == second
    assert first is not None
    assert first.schema == COVERAGE_SCHEMA
    assert first.source_path == "coverage/README.md"
    assert first.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.counts == {"MAPPED": 1, "DECOMPOSED": 1, "DEFERRED": 1, "OUT": 1}
    assert not first.complete
    assert first.to_json() == second.to_json()
    assert str(tmp_path) not in first.to_json()


def test_complete_means_every_in_scope_area_has_a_terminal_disposition(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    _contract(
        blueprint,
        "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md) |\n"
        "| Appendix | DEFERRED | Explicit later milestone |\n"
        "| Experiments | OUT | Narrative only |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert summary.complete


def test_rejects_unknown_duplicate_and_malformed_rows(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(
        blueprint,
        "| Main theorem | PARTIAL | Pending |\n"
        "| main THEOREM | MAPPED | Duplicate |\n"
        "| Missing evidence | DEFERRED | |\n"
        "| Too few | OUT |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    reasons = [issue.reason for issue in issues]
    assert any("unknown coverage disposition" in reason for reason in reasons)
    assert any("duplicate coverage area" in reason for reason in reasons)
    assert "coverage evidence is empty" in reasons
    assert "coverage row must have exactly three columns" in reasons


def test_ignores_fenced_tables_and_accepts_escaped_pipes(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    path = blueprint / "coverage/README.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Coverage\n\n"
        "```markdown\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Example | MAPPED | Placeholder |\n"
        "```\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main theorem | MAPPED | Case A \\| Case B |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert len(summary.entries) == 1
    assert summary.entries[0].evidence == "Case A | Case B"


def test_rejects_placeholder_and_unresolved_decomposed_evidence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    source = blueprint / "sources" / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Source\n", encoding="utf-8")
    _contract(
        blueprint,
        "| Placeholder | DEFERRED | TODO |\n"
        "| Missing | DECOMPOSED | [Missing](../roadmap/missing.md) |\n"
        "| Source only | DECOMPOSED | [Source](../sources/README.md) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    reasons = [issue.reason for issue in issues]
    assert "coverage evidence is a placeholder" in reasons
    assert reasons.count("DECOMPOSED coverage evidence has no link to an existing roadmap article") == 2


def test_evidence_validation_ignores_decorated_placeholders_and_fake_links(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "encoded article.md")
    _contract(
        blueprint,
        "| Decorated placeholder | DEFERRED | **TODO.** |\n"
        "| Inline code | DECOMPOSED | `[Fake](../roadmap/encoded%20article.md)` |\n"
        "| Comment | DECOMPOSED | <!-- [Fake](../roadmap/encoded%20article.md) --> |\n"
        "| Encoded | DECOMPOSED | [Real](<../roadmap/encoded%20article.md>) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    reasons = [issue.reason for issue in issues]
    assert "coverage evidence is a placeholder" in reasons
    assert reasons.count("DECOMPOSED coverage evidence must link to at least one roadmap article") == 2
    assert "DECOMPOSED coverage evidence has no link to an existing roadmap article" not in reasons


def test_malformed_evidence_paths_are_reported_without_raising(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(
        blueprint,
        "| Malformed | DECOMPOSED | [Bad](../roadmap/bad%00path.md) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "DECOMPOSED coverage evidence has no link to an existing roadmap article"
    ]


def test_reports_stable_unreadable_contract_issue(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    path = blueprint / "coverage/README.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage contract cannot be read as UTF-8"]
    assert str(tmp_path) not in issues[0].reason


def test_reports_missing_or_ambiguous_contract_table(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"

    summary, issues = load_coverage(blueprint)
    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage contract is missing"]

    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Coverage\n\nNo table yet.\n", encoding="utf-8")
    summary, issues = load_coverage(blueprint)
    assert summary is None
    assert "has no 'Area | Coverage | Evidence' table" in issues[0].reason
