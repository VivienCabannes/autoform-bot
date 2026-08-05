from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from autoform_cli.graph import load_graph
from autoform_cli.lean import build_linker, declaration_names
from autoform_cli.render import render_site
from autoform_cli.status import derive


_HREF = re.compile(r'href="([^"]+)"')
_EXAMPLE = Path("skills/setup/assets/cabannes-thesis-project")


def test_setup_asset_is_a_repo_shaped_thesis_vault(repo_root: Path) -> None:
    example = repo_root / _EXAMPLE
    blueprint = example / "blueprint"
    graph = load_graph(blueprint)

    assert set(graph.nodes) == {
        "infimum-loss/definitions/eligibility",
        "infimum-loss/definitions/non-ambiguity",
        "infimum-loss/theorems/infimum-loss",
        "infimum-loss/theorems/non-ambiguity-determinism",
        "infimum-loss/theorems/supervision-recovery",
    }
    assert graph.edge_count == 5
    eligibility = graph.nodes["infimum-loss/definitions/eligibility"]
    assert eligibility.declaration == "def"
    assert eligibility.statement_formalized
    assert eligibility.lean == "CabannesThesis.Eligible"
    recovery = graph.nodes["infimum-loss/theorems/supervision-recovery"]
    assert recovery.statement_dependencies == ("infimum-loss/theorems/infimum-loss",)
    assert recovery.proof_dependencies == ("infimum-loss/theorems/non-ambiguity-determinism",)

    # The example is a live demonstration of the distinction a flat status
    # field cannot make: supervision recovery is proved, but it rests on an
    # unproved node, so only its prerequisites earn the fully-proved colour.
    statuses = derive(graph)
    assert statuses["infimum-loss/theorems/supervision-recovery"].key == "proved"
    assert statuses["infimum-loss/theorems/infimum-loss"].key == "can_state"
    assert statuses["infimum-loss/definitions/eligibility"].key == "fully_proved"

    # Every declaration a node claims must exist in the project's Lean sources.
    linker = build_linker(example)
    for node in graph.nodes.values():
        for name in declaration_names(node.lean or ""):
            assert linker.location(name) is not None, f"{node.id}: {name}"

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
    site = tmp_path / "site-src"

    report = render_site(
        example / "blueprint",
        site,
        lean_root=example,
        repository_url="https://github.com/owner/repo",
        ref="0" * 40,
    )

    assert report.unresolved == []
    graph = load_graph(example / "blueprint")

    # Statements are published as environments on their milestone chapter,
    # each anchored so every cross-reference still lands on the statement.
    chapter_path = site / "roadmap/infimum-loss/README.md"
    chapter = chapter_path.read_text(encoding="utf-8")
    for node_id in graph.nodes:
        anchor = node_id.split("/", 1)[1].replace("/", "-")
        assert f'id="{anchor}"' in chapter, node_id
    assert not (site / "roadmap/infimum-loss/theorems").exists()

    # Both amsthm styles appear, and the status marks are derived.
    assert 'class="bp-thmwrapper theorem-style-definition bp-fully_proved"' in chapter
    assert 'class="bp-thmwrapper theorem-style-plain bp-proved"' in chapter

    for href in _HREF.findall(chapter):
        if href.startswith(("http", "#")):
            continue
        assert (chapter_path.parent / href.split("#")[0]).resolve().is_file(), href

    graph_page = (site / "dependencies.md").read_text(encoding="utf-8")
    assert "```mermaid" in graph_page
    for node_id in graph.nodes:
        anchor = node_id.split("/", 1)[1].replace("/", "-")
        # MkDocs publishes README.md as index.html, and a Mermaid fence is
        # never rewritten for us, so the click target must say so itself.
        assert f'"roadmap/infimum-loss/index.html#{anchor}"' in graph_page

    mkdocs = (example / "mkdocs.yml").read_text(encoding="utf-8")
    assert "docs_dir: site-src" in mkdocs
    assert "use_directory_urls: false" in mkdocs
    assert "md_in_html" in mkdocs
    assert "pymdownx.superfences" in mkdocs
    assert "stylesheets/blueprint.css" in mkdocs
    assert "javascripts/blueprint-mermaid.js" in mkdocs
    # A blue Bootstrap banner and no dark-mode toggle are both theme defaults.
    assert "nav_style: light" in mkdocs
    assert "user_color_mode_toggle: true" in mkdocs
    workflow = (example / ".github/workflows/blueprint-pages.yml").read_text(encoding="utf-8")
    assert "autoform check blueprint --lean-root ." in workflow
    assert "autoform render blueprint" in workflow
    assert "--require-declarations" in workflow
    assert "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128" in workflow
    assert "@main" not in workflow

    verify = (example / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
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
    roadmap_metadata = (repo_root / "skills/roadmap/agents/openai.yaml").read_text(encoding="utf-8")
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
        "kind: node",
        "blueprint/roadmap/**/*.md",
        "declaration",
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
