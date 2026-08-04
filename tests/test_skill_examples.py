from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import unquote

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

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

    assert (example / "README.md").is_file()
    assert (example / "CabannesThesis.lean").is_file()
    assert (example / "CabannesThesis/Basic.lean").is_file()
    toolchain = (example / "lean-toolchain").read_text(encoding="utf-8").strip()
    manifest = tomllib.loads((example / "lakefile.toml").read_text(encoding="utf-8"))
    assert toolchain == "leanprover/lean4:v4.32.2"
    assert manifest["require"][0]["rev"] == "v4.32.2"


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
    assert "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128" in workflow
    assert "@main" not in workflow

    verify = (example / ".github/workflows/autoform-verify.yml").read_text(
        encoding="utf-8"
    )
    assert "autoform check blueprint" in verify
    assert "lake build" in verify
    assert "Reject kernel-check bypass options" in verify
    assert "Audit every project declaration" in verify
    assert "Lean.collectAxioms" in verify
    assert "info.isUnsafe || info.isPartial" in verify
    assert 'forbidden="skip""KernelTC"' in verify
    assert 'git grep -n -I "$forbidden" -- .' in verify
    assert 'version: "0.12.1"' in verify
    assert "elan/releases/download/v4.2.3" in verify
    assert "df0b2b3a439961ffcbb3985214365ffe40f49bc871df04dff268c7d8e21ca8b2" in verify
    assert "github.ref == 'refs/heads/main'" in workflow
    assert 'version: "0.12.1"' in workflow
    assert "@main" not in verify

    for contents in (workflow, verify):
        action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s]+)", contents)
        assert action_refs
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_each_skill_points_to_its_thesis_example(repo_root: Path) -> None:
    setup = (repo_root / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    setup_metadata = (repo_root / "skills/setup/agents/openai.yaml").read_text(encoding="utf-8")
    roadmap = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8")
    roadmap_metadata = (repo_root / "skills/roadmap/agents/openai.yaml").read_text(
        encoding="utf-8"
    )
    orchestrate = (repo_root / "skills/orchestrate/SKILL.md").read_text(encoding="utf-8")
    review = (repo_root / "skills/review/SKILL.md").read_text(encoding="utf-8")

    for required in (
        "assets/cabannes-thesis-project/README.md",
        "lean-toolchain",
        "autoform-verify.yml",
        "Obsidian",
        "GitHub Pages",
    ):
        assert required in setup
    for required in (
        "references/cabannes-thesis-roadmap.md",
        "blueprint/roadmap/",
        "blueprint/coverage/",
        "blueprint/nodes/**/*.md",
        "coarse roadmap",
        "## Depends on",
    ):
        assert required in roadmap
    assert "references/thesis-worked-node.md" in orchestrate
    assert "references/thesis-review-case.md" in review
    assert "$setup" in setup_metadata
    assert "$roadmap" in roadmap_metadata
    assert "stops before\nmathematical planning" in setup
    assert "Do not\nscan for undecomposed chapters" in orchestrate
    assert (repo_root / "skills/roadmap/references/cabannes-thesis-roadmap.md").is_file()
    assert (repo_root / "skills/orchestrate/references/thesis-worked-node.md").is_file()
    assert (repo_root / "skills/review/references/thesis-review-case.md").is_file()
