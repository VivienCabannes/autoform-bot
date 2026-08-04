from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from autoform_cli.graph import GraphValidationError, load_graph


def _node(blueprint: Path, relative: str, body: str) -> Path:
    path = blueprint / "nodes" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_nested_wiki_and_metadata(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "foundations/base.md",
        """---
kind: theorem
status: proved
lean: Autoform.Base
---
# Base theorem
""",
    )
    _node(
        blueprint,
        "result.md",
        """# Main result

[This ordinary link is ignored](foundations/missing.md)

## Depends on

- [Base](foundations/base.md#proof)
- [Base again](foundations/base.md)

## Notes

[Also ignored](missing.md)
""",
    )

    graph = load_graph(blueprint)

    assert list(graph.nodes) == ["foundations/base", "result"]
    assert graph.nodes["foundations/base"].title == "Base theorem"
    assert graph.nodes["foundations/base"].kind == "theorem"
    assert graph.nodes["foundations/base"].status == "proved"
    assert graph.nodes["foundations/base"].lean == "Autoform.Base"
    assert graph.nodes["result"].dependencies == ("foundations/base",)
    assert graph.edge_count == 1


def test_resolves_links_relative_to_each_node(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    _node(
        blueprint,
        "chapter/result.md",
        "# Result\n\n## Depends on\n\n[Base](../base.md)\n",
    )

    graph = load_graph(blueprint)

    assert graph.nodes["chapter/result"].dependencies == ("base",)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("No title\n", "missing H1 title"),
        ("# First title\n# Second title\n", "multiple H1 titles"),
        ("---\nkind: theorem\n# Title\n", "unterminated frontmatter"),
        ("---\nowner: me\n---\n# Title\n", "unsupported frontmatter key"),
        ("# Result\n## Depends on\n[x](missing.md)\n", "does not exist"),
        ("# Result\n## Depends on\n[x](note.txt)\n", "relative .md file"),
        ("# Result\n## Depends on\n[x](https://example.com/x.md)\n", "relative Markdown path"),
        ("# Result\n## Depends on\n[x](../../outside.md)\n", "escapes the nodes directory"),
    ],
)
def test_rejects_invalid_nodes(tmp_path: Path, body: str, message: str) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "result.md", body)
    (tmp_path / "outside.md").write_text("# Outside\n", encoding="utf-8")

    with pytest.raises(GraphValidationError, match=message):
        load_graph(blueprint)


def test_rejects_self_edge(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "self.md", "# Self\n## Depends on\n[Self](self.md)\n")

    with pytest.raises(GraphValidationError, match="dependency on itself"):
        load_graph(blueprint)


def test_rejects_cycle(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "a.md", "# A\n## Depends on\n[B](b.md)\n")
    _node(blueprint, "b.md", "# B\n## Depends on\n[A](a.md)\n")

    with pytest.raises(GraphValidationError, match=r"dependency cycle: a -> b -> a"):
        load_graph(blueprint)


def test_ignores_links_in_code_fences_and_other_sections(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(
        blueprint,
        "only.md",
        """# Only

## Notes
[Missing](missing.md)

## Depends on
`[Inline example](missing.md)`
<!-- [Commented out](missing.md) -->
<!--
[Multiline comment](missing.md)
-->
```markdown
[Also missing](missing.md)
```
""",
    )

    assert load_graph(blueprint).nodes["only"].dependencies == ()


def test_requires_blueprint_and_nodes_directories(tmp_path: Path) -> None:
    with pytest.raises(GraphValidationError, match="blueprint directory does not exist"):
        load_graph(tmp_path / "absent")

    blueprint = tmp_path / "blueprint"
    blueprint.mkdir()
    with pytest.raises(GraphValidationError, match="nodes directory does not exist"):
        load_graph(blueprint)


def test_check_cli(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md", "# Base\n")
    result = subprocess.run(
        [sys.executable, "-m", "autoform_cli", "check", str(blueprint)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "OK: 1 nodes, 0 dependencies"


def test_check_cli_reports_validation_errors(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "bad.md", "no heading\n")
    result = subprocess.run(
        [sys.executable, "-m", "autoform_cli", "check", str(blueprint)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "error: bad: missing H1 title" in result.stdout
