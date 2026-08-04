from __future__ import annotations

from pathlib import Path

import pytest

from visualization.export_graph import export_graph, main


def _write_node(path: Path, title: str, dependencies: list[tuple[str, str]] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}"]
    if dependencies:
        lines.extend(["", "## Depends on", ""])
        lines.extend(f"- [{label}]({target})" for label, target in dependencies)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_export_is_self_contained_and_links_to_markdown(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "nodes" / "foundations" / "base lemma.md", "Base lemma")
    _write_node(
        blueprint / "nodes" / "main.md",
        "Main <result>",
        [("Base lemma", "foundations/base%20lemma.md#statement")],
    )

    output = export_graph(blueprint)
    document = output.read_text(encoding="utf-8")

    assert output == (blueprint / "graph.html").resolve()
    assert 'href="nodes/foundations/base%20lemma.md"' in document
    assert 'href="nodes/main.md"' in document
    assert 'data-prerequisite="foundations/base lemma" data-dependent="main"' in document
    assert "Main &lt;result&gt;" in document
    assert "arrows point from prerequisite to dependent" in document
    assert "<script" not in document
    assert "https://" not in document


def test_cli_writes_an_explicit_destination(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "nodes" / "only.md", "Only node")
    output = tmp_path / "site" / "dependencies.html"

    assert main([str(blueprint), "-o", str(output)]) == 0

    assert output.is_file()
    assert str(output.resolve()) in capsys.readouterr().out
    assert 'href="../blueprint/nodes/only.md"' in output.read_text(encoding="utf-8")


def test_cli_reports_invalid_blueprint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        main([str(tmp_path / "missing")])

    assert "blueprint directory does not exist" in capsys.readouterr().err
