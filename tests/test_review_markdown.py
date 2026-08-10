"""The review surface reads the Markdown wiki, not ``graph.json``.

These cover the projection itself and the two places where the old two-level
JSON schema encoded "this is a reviewable unit" as ``tier == 2``, which stops
being true once a roadmap nests to arbitrary depth.
"""
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm        # noqa: E402
import serve_review as sv        # noqa: E402

from autoform_cli.runtime import load_runtime_model  # noqa: E402


def _article(blueprint: Path, relative: str, *, title: str, body: str = "",
             depends: tuple[str, ...] = (), **metadata: str) -> None:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", *(f"{k}: {v}" for k, v in metadata.items()), "---", "",
             f"# {title}", "", body or "A precise statement."]
    if depends:
        lines += ["", "## Depends on", "", *(f"- [dep]({t})" for t in depends)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _wiki(tmp_path: Path) -> Path:
    """A three-level roadmap: book / chapter / section / statement."""
    blueprint = tmp_path / "project" / "blueprint"
    _article(blueprint, "README.md", title="Book")
    _article(blueprint, "analysis/README.md", title="Analysis")
    _article(blueprint, "analysis/limits/README.md", title="Limits")
    _article(blueprint, "analysis/limits/squeeze.md", title="Squeeze theorem",
             declaration="theorem")
    _article(blueprint, "analysis/limits/bounded.md", title="Bounded sequences",
             declaration="lemma", depends=("squeeze.md",))
    return blueprint


def test_the_wiki_projects_into_the_review_shape(tmp_path: Path) -> None:
    nodes, metadata = rm.load_graph(_wiki(tmp_path))

    assert metadata["authority"] == "markdown-articles"
    assert metadata["source_revision"]
    # The root article is the book's preface, not a reviewable unit.
    assert set(nodes) == {
        "analysis", "analysis/limits",
        "analysis/limits/squeeze", "analysis/limits/bounded",
    }
    squeeze = nodes["analysis/limits/squeeze"]
    assert squeeze["name"] == "Squeeze theorem"
    assert squeeze["kind"] == "theorem"
    assert squeeze["parent"] == "analysis/limits"
    assert squeeze["formalizable"]
    assert nodes["analysis/limits/bounded"]["depends_on"] == ["analysis/limits/squeeze"]


def test_a_project_root_and_its_blueprint_load_the_same(tmp_path: Path) -> None:
    blueprint = _wiki(tmp_path)

    assert rm.load_graph(blueprint) == rm.load_graph(blueprint.parent)


def test_tiers_follow_containment_at_any_depth(tmp_path: Path) -> None:
    """The old schema had exactly two levels; a wiki nests as deep as it likes."""
    nodes, _metadata = rm.load_graph(_wiki(tmp_path))

    assert rm.tiers_present(nodes) == [1, 2, 3]
    assert nodes["analysis"]["parent"] is None
    assert rm.child_ids("analysis", nodes) == ["analysis/limits"]
    assert rm.child_ids("analysis/limits", nodes) == [
        "analysis/limits/bounded", "analysis/limits/squeeze",
    ]


def _clean(*node_ids: str) -> dict:
    sidecar = rm.empty_sidecar()
    sidecar["reviews"] = {
        nid: {"ai": {"faithfulness": 5, "proof_integrity": 5, "code_quality": 5,
                     "verdict": "clean"}}
        for nid in node_ids
    }
    return sidecar


def test_reviewable_units_are_the_formalizable_ones_not_tier_two(tmp_path: Path) -> None:
    """The statements here are tier 3, so counting tier 2 would report zero."""
    nodes, _metadata = rm.load_graph(_wiki(tmp_path))

    assert rm.coverage(nodes, rm.empty_sidecar())["total"] == 2

    # `bounded` depends on `squeeze`, so it is the only sink; it reaches the
    # frontier once its whole closure is trusted, and not before.
    both = _clean("analysis/limits/squeeze", "analysis/limits/bounded")
    assert rm.trust_frontier(nodes, both) == ["analysis/limits/bounded"]
    assert rm.trust_frontier(nodes, _clean("analysis/limits/bounded")) == []


def test_legacy_json_still_counts_tier_two_as_reviewable() -> None:
    """A JSON fixture carries no `formalizable`, so the tier fallback holds."""
    nodes = {
        "cluster": {"id": "cluster", "tier": 1, "parent": None, "kind": "section"},
        "thm": {"id": "thm", "tier": 2, "parent": "cluster", "kind": "theorem",
                "mathlib_status": "missing", "depends_on": []},
    }

    assert rm.coverage(nodes, rm.empty_sidecar())["total"] == 1


def test_a_directory_that_is_not_a_blueprint_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    with pytest.raises(Exception, match="roadmap directory does not exist"):
        load_runtime_model(tmp_path / "empty")


# --- the server surface -----------------------------------------------------

def test_the_server_derives_project_paths_from_the_blueprint(tmp_path: Path) -> None:
    """`blueprint/` sits in the project root, exactly where `graph.json` did."""
    blueprint = _wiki(tmp_path)
    project = blueprint.parent

    proj = sv.Project(blueprint)

    assert proj.root == project
    assert proj.sidecar_path == project / "review_status.json"
    assert proj.content_dir == project / "informal_content"
    assert proj.metadata()["authority"] == "markdown-articles"
    assert set(proj.nodes()) == {
        "analysis", "analysis/limits",
        "analysis/limits/squeeze", "analysis/limits/bounded",
    }


def test_the_server_renders_every_tier_of_the_wiki(tmp_path: Path) -> None:
    proj = sv.Project(_wiki(tmp_path))

    for tier, expected in ((1, "analysis"), (3, "analysis/limits/squeeze")):
        page = sv.render_home(proj, tier=tier).decode("utf-8")
        assert expected in page


def test_the_server_cli_takes_a_blueprint_or_a_legacy_graph(tmp_path: Path) -> None:
    blueprint = _wiki(tmp_path)

    with pytest.raises(SystemExit):                       # neither source given
        sv.main(["--port", "0"])
    with pytest.raises(SystemExit):                       # both sources given
        sv.main(["--blueprint", str(blueprint), "--graph", "g.json", "--port", "0"])
    with pytest.raises(SystemExit):                       # not a blueprint
        sv.main(["--blueprint", str(tmp_path), "--port", "0"])
