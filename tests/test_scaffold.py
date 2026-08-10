"""The vault layout is fixed, so the tool writes it rather than describing it.

A real project came back from an agent-driven setup with chapter pages as
siblings of their directories instead of ``<chapter>/README.md``. That parses
clean and publishes a book with no chapters, so these tests pin the shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.graph import load_graph
from autoform_cli.scaffold import ScaffoldError, scaffold_project

_EXPECTED = {
    ".github/workflows/autoform-verify.yml",
    ".github/workflows/blueprint-pages.yml",
    ".gitignore",
    "README.md",
    "blueprint/.gitignore",
    "blueprint/README.md",
    "blueprint/coverage/README.md",
    "blueprint/javascripts/mathjax.js",
    "blueprint/roadmap/README.md",
    "blueprint/sources/README.md",
    "mkdocs.yml",
    "theme/main.html",
}


def test_scaffold_writes_the_whole_vault(tmp_path: Path) -> None:
    result = scaffold_project(tmp_path, title="Finite Flat", repository_url="https://example.test/repo")

    assert set(result.written) == _EXPECTED
    assert result.skipped == ()
    for relative in _EXPECTED:
        assert (tmp_path / relative).is_file(), relative


def test_scaffolded_vault_validates_immediately(tmp_path: Path) -> None:
    """A fresh project must pass `autoform check` before any mathematics."""

    scaffold_project(tmp_path, title="Finite Flat")
    graph = load_graph(tmp_path / "blueprint")

    assert set(graph.nodes) == {"roadmap"}
    assert graph.nodes["roadmap"].parent is None


def test_substitutions_reach_the_site_config(tmp_path: Path) -> None:
    scaffold_project(
        tmp_path,
        title="Finite Flat",
        repository_url="https://example.test/repo",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="0" * 40,
    )

    mkdocs = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    assert "site_name: Finite Flat" in mkdocs
    assert "repo_url: https://example.test/repo" in mkdocs

    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f"git+https://example.test/autoform.git@{'0' * 40}" in verify


def test_no_placeholder_survives_anywhere(tmp_path: Path) -> None:
    """`${{ }}` is Actions syntax and `{{declName}}` is Lean interpolation.

    Only our own UPPER_SNAKE placeholders must be gone.
    """
    import re

    placeholder = re.compile(r"\{\{[A-Z_]+\}\}")
    scaffold_project(tmp_path, title="Finite Flat")

    for path in sorted(tmp_path.rglob("*")):
        if path.is_file():
            assert not placeholder.search(path.read_text(encoding="utf-8")), path


def test_rerun_is_idempotent_and_reports_what_it_left(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")
    (tmp_path / "blueprint/README.md").write_text("# Hand written\n", encoding="utf-8")

    again = scaffold_project(tmp_path, title="Finite Flat")

    assert again.written == ()
    assert set(again.skipped) == _EXPECTED
    assert (tmp_path / "blueprint/README.md").read_text(encoding="utf-8") == "# Hand written\n"


def test_force_overwrites(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")
    (tmp_path / "mkdocs.yml").write_text("stale\n", encoding="utf-8")

    scaffold_project(tmp_path, title="Finite Flat", force=True)

    assert "site_name: Finite Flat" in (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")


def test_refuses_an_empty_title(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match="title must not be empty"):
        scaffold_project(tmp_path, title="   ")


def test_refuses_a_symlinked_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ScaffoldError, match="symlink"):
        scaffold_project(link, title="Finite Flat")


def test_cli_reports_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from autoform_cli.__main__ import main

    assert main(["init", str(tmp_path), "--title", "Finite Flat", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["project"] == "Finite Flat"
    assert set(payload["written"]) == _EXPECTED


def test_roadmap_readme_teaches_the_chapter_shape(tmp_path: Path) -> None:
    """The exact mistake this command exists to prevent must be named in it."""

    scaffold_project(tmp_path, title="Finite Flat")
    roadmap = (tmp_path / "blueprint/roadmap/README.md").read_text(encoding="utf-8")

    assert "<chapter>/README.md" in roadmap or "README.md" in roadmap
    assert "without a `README.md` is not a chapter" in roadmap


def test_scaffolded_theme_defers_navigation_to_the_book(tmp_path: Path) -> None:
    """Autoform derives reading order from the vault, so MkDocs must not.

    This used to be prose in the Setup skill telling an agent to strip the
    global previous/next controls. It is now a property of the file we write.
    """

    scaffold_project(tmp_path, title="Finite Flat")
    theme = (tmp_path / "theme/main.html").read_text(encoding="utf-8")
    mkdocs = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")

    assert "{% block next_prev %}{% endblock %}" in theme
    assert "docs_dir: site-src" in mkdocs
    assert "md_in_html" in mkdocs
    assert "custom_dir: theme" in mkdocs
