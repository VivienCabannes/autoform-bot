from __future__ import annotations

import re
from pathlib import Path

import pytest

from autoform_cli import mermaid
from autoform_cli.graph import load_graph
from autoform_cli.status import STATES, derive
from autoform_cli.visualize import export_graph, export_structure, main


def _state(key: str):
    return next(state for state in STATES if state.key == key)


def _write_node(
    path: Path,
    title: str,
    dependencies: list[tuple[str, str]] | None = None,
    **metadata: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    properties = [*(f"{key}: {value}" for key, value in metadata.items())]
    lines = ["---", *properties, "---", "", f"# {title}"]
    if dependencies:
        lines.extend(["", "## Depends on", ""])
        lines.extend(f"- [{label}]({target})" for label, target in dependencies)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_export_writes_a_mermaid_page_linking_to_markdown(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "foundations" / "README.md", "Foundations")
    _write_node(blueprint / "roadmap" / "foundations" / "base lemma.md", "Base lemma")
    _write_node(
        blueprint / "roadmap" / "main.md",
        "Main <result>",
        [("Base lemma", "foundations/base%20lemma.md#statement")],
    )

    output = export_graph(blueprint)
    document = output.read_text(encoding="utf-8")

    assert output == (blueprint / "dependencies.md").resolve()
    assert "```mermaid" in document
    assert "graph LR" in document
    # Handles are assigned in sorted order, so pin the links and the edge by
    # their targets rather than by whichever index a node happens to get.
    handles = dict(re.findall(r'click (n\d+) "([^"]+)"', document))
    lemma = next(k for k, v in handles.items() if v == "roadmap/foundations/base lemma.md")
    main = next(k for k, v in handles.items() if v == "roadmap/main.md")
    assert f"  {lemma} --> {main}" in document
    assert "Main <result>" in document


def test_diagram_colours_and_shapes_follow_derived_status(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(
        blueprint / "roadmap" / "base.md",
        "Base",
        declaration="def",
        statement="formalized",
    )
    _write_node(
        blueprint / "roadmap" / "top.md",
        "Top",
        [("Base", "base.md")],
        declaration="theorem",
        statement="formalized",
        proof="formalized",
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    # Definitions are rectangles, propositions are rounded.
    assert 'n0["Base"]:::fully_proved' in document
    assert 'n1("Top"):::fully_proved' in document
    # The vault copy carries its palette inline; Obsidian has no init script.
    assert f"classDef fully_proved fill:{_state('fully_proved').fill}" in document
    assert '<span class="bp-swatch bp-swatch-fully_proved">' in document
    assert '<span class="bp-legend-count">2</span>' in document


def test_the_published_graph_defers_its_palette_to_the_theme(tmp_path: Path) -> None:
    """Mermaid scopes its styles to the SVG id, so dark mode must re-render."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only", declaration="theorem")
    graph = load_graph(blueprint)
    output = tmp_path / "dependencies.md"

    published = mermaid.render_page(
        graph, derive(graph), output, links={"only": "x.html#only"}, include_classdefs=False
    )

    assert ":::can_state" in published
    assert "classDef" not in published


def test_proof_only_dependencies_are_dashed(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "tool.md", "Tool")
    (blueprint / "roadmap" / "result.md").write_text(
        "---\n---\n\n# Result\n\n## Proof depends on\n\n- [Tool](tool.md)\n",
        encoding="utf-8",
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "  n1 -.-> n0" in document
    assert "  n1 --> n0" not in document


def test_green_stops_at_an_unproved_prerequisite(tmp_path: Path) -> None:
    """The distinction a flat status field cannot express."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "gap.md", "Gap", declaration="theorem")
    _write_node(
        blueprint / "roadmap" / "top.md",
        "Top",
        [("Gap", "gap.md")],
        declaration="theorem",
        statement="formalized",
        proof="formalized",
    )

    statuses = derive(load_graph(blueprint))

    assert statuses["top"].proved
    assert not statuses["top"].fully_proved
    assert statuses["top"].key == "proved"


def test_cli_writes_an_explicit_destination(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")
    output = tmp_path / "docs" / "dependencies.md"

    assert main([str(blueprint), "-o", str(output)]) == 0

    assert output.is_file()
    assert str(output.resolve()) in capsys.readouterr().out
    assert 'click n0 "../blueprint/roadmap/only.md"' in output.read_text(encoding="utf-8")


def test_cli_accepts_html_link_extension(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")
    output = blueprint / "dependencies.md"

    assert main([str(blueprint), "--output", str(output), "--link-extension", ".html"]) == 0

    assert str(output.resolve()) in capsys.readouterr().out
    assert 'click n0 "roadmap/only.html"' in output.read_text(encoding="utf-8")


def test_cli_reports_invalid_blueprint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        main([str(tmp_path / "missing")])

    assert "blueprint directory does not exist" in capsys.readouterr().err


def test_the_vault_gets_a_structure_page_obsidian_can_read(tmp_path: Path) -> None:
    """Obsidian shows the tree but cannot derive a node's state from the file.

    Plain Markdown only: the site stylesheet does not exist in a vault, so the
    HTML grid the published page uses would render as a wall of tags here.
    """
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "part" / "README.md", "Part")
    _write_node(
        blueprint / "roadmap" / "part" / "base.md",
        "Base",
        declaration="def",
        statement="formalized",
    )

    output = export_structure(blueprint)

    assert output == blueprint / "structure.md"
    page = output.read_text(encoding="utf-8")
    assert "- **roadmap/**" in page
    assert "    - **part/**" in page
    assert "[Base](roadmap/part/base.md) \u00b7 def \u00b7 fully proved" in page
    assert "<" not in page.split("---", 2)[2]
    # It never lists itself, and never the other generated view.
    assert "structure.md)" not in page
    assert "dependencies.md)" not in page


def test_the_vault_structure_page_warns_about_a_flat_roadmap(tmp_path: Path) -> None:
    """The one fault the file explorer cannot show, as an Obsidian callout."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    for name in ("a", "b", "c", "d"):
        _write_node(blueprint / "roadmap" / f"{name}.md", f"Result {name}", declaration="theorem")

    page = export_structure(blueprint).read_text(encoding="utf-8")

    assert "> [!warning] Every article sits directly under `roadmap/`." in page
