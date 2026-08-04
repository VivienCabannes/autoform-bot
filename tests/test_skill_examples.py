from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import unquote

from autoform_cli.graph import load_graph
from autoform_cli.visualize import export_graph


_HREF = re.compile(r'href="([^"]+)"')
_EXAMPLE = Path("skills/setup/assets/cabannes-thesis-project")


def test_setup_asset_is_a_repo_shaped_thesis_vault(repo_root: Path) -> None:
    example = repo_root / _EXAMPLE
    blueprint = example / "blueprint"
    graph = load_graph(blueprint)

    assert set(graph.nodes) == {
        "definitions/eligibility",
        "definitions/non-ambiguity",
        "theorems/infimum-loss",
        "theorems/non-ambiguity-determinism",
        "theorems/supervision-recovery",
    }
    assert graph.edge_count == 4
    assert graph.nodes["definitions/eligibility"].status == "ready"
    assert graph.nodes["theorems/supervision-recovery"].dependencies == (
        "theorems/infimum-loss",
        "theorems/non-ambiguity-determinism",
    )

    assert (blueprint / "roadmap" / "README.md").is_file()
    assert (blueprint / "coverage" / "README.md").is_file()
    source = (blueprint / "sources" / "thesis.md").read_text(encoding="utf-8")
    assert "arXiv:2209.11629" in source
    assert "infimum/core.tex" in source
    assert "il:thm:ambiguity" in source
    assert "il:thm:non-ambiguity" in source
    assert ".obsidian/" in (blueprint / ".gitignore").read_text(encoding="utf-8")

    overview = (blueprint / "README.md").read_text(encoding="utf-8")
    assert "kind: blueprint" in overview
    assert "status: active" in overview
    assert "[Roadmap](roadmap/README.md)" in overview
    assert "[Coverage](coverage/README.md)" in overview


def test_setup_asset_static_site_contract(repo_root: Path, tmp_path: Path) -> None:
    example = repo_root / _EXAMPLE
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
    workflow = (example / ".github/workflows/blueprint-pages.yml").read_text(encoding="utf-8")
    assert "autoform check blueprint" in workflow
    assert "--link-extension .html" in workflow
    assert "actions/deploy-pages@v4" in workflow


def test_each_skill_points_to_its_thesis_example(repo_root: Path) -> None:
    setup = (repo_root / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    setup_metadata = (repo_root / "skills/setup/agents/openai.yaml").read_text(encoding="utf-8")
    orchestrate = (repo_root / "skills/orchestrate/SKILL.md").read_text(encoding="utf-8")
    review = (repo_root / "skills/review/SKILL.md").read_text(encoding="utf-8")

    for required in (
        "references/thesis-blueprint.md",
        "assets/cabannes-thesis-project/blueprint/README.md",
        "roadmap/",
        "coverage/",
        "nodes/",
        "Obsidian",
        "GitHub Pages",
    ):
        assert required in setup
    assert "references/thesis-worked-node.md" in orchestrate
    assert "references/thesis-review-case.md" in review
    assert "$setup" in setup_metadata
    assert (repo_root / "skills/setup/references/thesis-blueprint.md").is_file()
    assert (repo_root / "skills/orchestrate/references/thesis-worked-node.md").is_file()
    assert (repo_root / "skills/review/references/thesis-review-case.md").is_file()
