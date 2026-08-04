from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import unquote

from autoform_cli.graph import load_graph
from autoform_cli.visualize import export_graph


_HREF = re.compile(r'href="([^"]+)"')


def test_example_is_a_repo_shaped_obsidian_vault(repo_root: Path) -> None:
    example = repo_root / "examples"
    blueprint = example / "blueprint"
    graph = load_graph(blueprint)

    assert set(graph.nodes) == {
        "definitions/convex-set",
        "lemmas/supporting-hyperplane",
        "theorems/separation",
    }
    assert (blueprint / "roadmap" / "README.md").is_file()
    assert (blueprint / "coverage" / "separation.md").is_file()
    assert (blueprint / "sources" / "convexity.md").is_file()
    assert ".obsidian/" in (blueprint / ".gitignore").read_text(encoding="utf-8")

    overview = (blueprint / "README.md").read_text(encoding="utf-8")
    assert "kind: blueprint" in overview
    assert "status: active" in overview
    assert "[Roadmap](roadmap/README.md)" in overview
    assert "[Coverage](coverage/separation.md)" in overview


def test_example_static_site_contract(repo_root: Path, tmp_path: Path) -> None:
    example = repo_root / "examples"
    blueprint = tmp_path / "blueprint"
    shutil.copytree(example / "blueprint", blueprint)
    output = blueprint / "dependencies.html"

    export_graph(blueprint, output, link_extension=".html")
    document = output.read_text(encoding="utf-8")

    node_hrefs = [unquote(href) for href in _HREF.findall(document) if href.endswith(".html")]
    assert len(node_hrefs) == len(load_graph(blueprint).nodes)
    for href in node_hrefs:
        linked_source = (output.parent / href).with_suffix(".md").resolve()
        linked_source.relative_to(blueprint.resolve())
        assert linked_source.is_file()

    mkdocs = (example / "mkdocs.yml").read_text(encoding="utf-8")
    assert "docs_dir: blueprint" in mkdocs
    assert "use_directory_urls: false" in mkdocs
    workflow = (example / ".github" / "workflows" / "blueprint-pages.yml").read_text(encoding="utf-8")
    assert "autoform check blueprint" in workflow
    assert "--link-extension .html" in workflow
    assert "actions/deploy-pages@v4" in workflow


def test_setup_skill_points_agents_to_the_example(repo_root: Path) -> None:
    skill = (repo_root / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    metadata = (repo_root / "skills" / "setup" / "agents" / "openai.yaml").read_text(encoding="utf-8")

    for required in ("examples/", "roadmap/", "coverage/", "nodes/", "Obsidian", "GitHub Pages"):
        assert required in skill
    assert "Preserve and merge" in skill
    assert "$setup" in metadata
