from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.lean import _normalize_remote
from autoform_cli.render import render_site


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (project / "Project").mkdir()
    (project / "Project" / "Basic.lean").write_text(
        "namespace Project\n\ndef Base : Nat := 0\n\ntheorem top : True := trivial\n\nend Project\n",
        encoding="utf-8",
    )
    (project / "blueprint" / "README.md").write_text(
        "---\nkind: blueprint\n---\n\n# Overview\n", encoding="utf-8"
    )
    (roadmap / "base.md").write_text(
        "---\nkind: node\ndeclaration: def\nstatement: formalized\nlean: Project.Base\n---\n\n"
        "# Base\n\nThe base object.\n\n## Depends on\n\nThis node has no prerequisites.\n",
        encoding="utf-8",
    )
    (roadmap / "top.md").write_text(
        "---\nkind: node\ndeclaration: theorem\nstatement: formalized\nproof: formalized\n"
        "lean: Project.top\ndiscussion: 42\n---\n\n"
        "# Top\n\nThe main result.\n\n## Sources\n\n- [Paper](../sources.md)\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    return project


def _render(tmp_path: Path, **kwargs):
    project = _project(tmp_path)
    report = render_site(
        project / "blueprint",
        tmp_path / "out",
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="cafe1234",
        **kwargs,
    )
    return project, report


def test_render_writes_a_derived_tree_and_leaves_the_vault_alone(tmp_path: Path) -> None:
    project, report = _render(tmp_path)
    out = tmp_path / "out"

    assert report.nodes == 2
    assert report.linked == 2
    assert report.unresolved == []
    assert (out / "dependencies.md").is_file()
    assert (out / "stylesheets/blueprint.css").is_file()
    assert (out / "javascripts/blueprint-mermaid.js").is_file()
    # The source vault keeps no generated files.
    assert not (project / "blueprint" / "dependencies.md").exists()
    assert "## Depends on" in (project / "blueprint/roadmap/top.md").read_text(encoding="utf-8")


def test_node_pages_become_numbered_statement_boxes(tmp_path: Path) -> None:
    _render(tmp_path)
    page = (tmp_path / "out/roadmap/top.md").read_text(encoding="utf-8")

    assert "title: Theorem 1 (Top)" in page
    assert '<div class="bp-node bp-fully_proved" markdown="1">' in page
    assert '<p class="bp-chip">fully proved</p>' in page
    assert "# Theorem 1 (Top)" in page
    assert "The main result." in page
    # Unrelated sections survive; the DAG moves into the footer.
    assert "## Sources" in page
    assert "## Depends on" not in page


def test_the_footer_links_code_prerequisites_and_dependents(tmp_path: Path) -> None:
    _render(tmp_path)
    top = (tmp_path / "out/roadmap/top.md").read_text(encoding="utf-8")
    base = (tmp_path / "out/roadmap/base.md").read_text(encoding="utf-8")

    assert "https://github.com/owner/repo/blob/cafe1234/Project/Basic.lean#L5" in top
    assert '<span class="bp-key">Uses</span>' in top
    assert 'href="base.html">Definition 1 (Base)' in top
    assert 'href="https://github.com/owner/repo/issues/42">#42' in top
    assert '<span class="bp-key">Used by</span>' in base
    assert 'href="top.html">Theorem 1 (Top)' in base


def test_unresolved_declarations_are_reported_not_linked(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\nkind: node\ndeclaration: theorem\nstatement: formalized\nlean: Project.absent\n---\n"
        "\n# Top\n",
        encoding="utf-8",
    )
    report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert report.unresolved == ["top: Project.absent"]
    page = (tmp_path / "out/roadmap/top.md").read_text(encoding="utf-8")
    assert "not found in the Lean sources" in page


def test_stale_generated_files_are_not_republished(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/dependencies.html").write_text("stale", encoding="utf-8")
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out/dependencies.html").exists()
    assert (tmp_path / "out/dependencies.md").is_file()


def test_refuses_to_render_over_the_vault(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="over itself"):
        render_site(project / "blueprint", project / "blueprint")


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo/", "https://github.com/owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "https://github.com/owner/repo"),
        ("/local/path", None),
    ],
)
def test_git_remotes_normalize_to_web_urls(remote: str, expected: str | None) -> None:
    assert _normalize_remote(remote) == expected
