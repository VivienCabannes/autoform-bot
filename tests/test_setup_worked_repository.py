"""Contract tests for Setup's current-architecture worked repository."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "skills" / "setup" / "assets" / "worked-formalization-project"
COMMIT = "c" * 40

for directory in (ROOT / "scripts", ROOT / "scripts" / "review_ui"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import export_github_dashboard as exporter  # noqa: E402
import review_model as review  # noqa: E402


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_worked_repository_maps_broadly_and_decomposes_one_milestone() -> None:
    nodes, metadata = review.load_graph(ASSET / "graph.json")
    assert metadata["scope"] == {
        "mapped": ["Parity core", "Divisibility extensions", "Congruence examples"],
        "milestone": "Parity core",
    }
    assert len(nodes) == 6
    assert [node_id for node_id, node in nodes.items() if node["tier"] == 1] == [
        "Parity foundations"
    ]
    tier_two = {node_id for node_id, node in nodes.items() if node["tier"] == 2}
    assert tier_two == {
        "Even integer",
        "Integer linear combination",
        "Sum of even integers",
        "Scaling preserves evenness",
        "Even linear combinations",
    }
    assert nodes["Even linear combinations"]["depends_on"] == [
        "Integer linear combination",
        "Sum of even integers",
        "Scaling preserves evenness",
    ]
    assert all(nodes[node_id]["parent"] == "Parity foundations" for node_id in tier_two)

    for node_id, node in nodes.items():
        content = ASSET / node["content"]
        assert content.is_file(), node_id
        for source_ref in node["source_refs"]:
            source = ASSET / source_ref["file"]
            assert source.is_file(), (node_id, source_ref)
            assert source_ref["location"] in source.read_text(encoding="utf-8")
        if lean_file := node.get("lean_file"):
            assert not Path(lean_file).is_absolute()
            assert (ASSET / lean_file).is_file()

    coverage = (ASSET / "coverage.md").read_text(encoding="utf-8")
    assert "`DECOMPOSED`" in coverage
    assert "`MAPPED`" in coverage
    assert "`OUT`" in coverage


def test_worked_repository_has_reviewed_placeholder_free_lean_and_kernel_evidence() -> None:
    nodes, _metadata = review.load_graph(ASSET / "graph.json")
    lean = (ASSET / "WorkedExample" / "Parity.lean").read_text(encoding="utf-8")
    assert not review.lean_has_incomplete_proof(lean)
    for declaration in (
        "WorkedExample.isEven_add",
        "WorkedExample.isEven_mul_left",
        "WorkedExample.linearCombination_isEven",
    ):
        assert declaration.rsplit(".", 1)[1] in lean

    sidecar = review.load_sidecar(ASSET / "review_status.json")
    for node_id in nodes:
        if nodes[node_id]["tier"] == 2:
            assert review.verdict_of(node_id, sidecar) == "clean"
    for node_id in (
        "Sum of even integers",
        "Scaling preserves evenness",
        "Even linear combinations",
    ):
        evidence = (ASSET / "kernel" / f"{node_id}.txt").read_text(encoding="utf-8")
        assert "depends on axioms: []" in evidence


def test_worked_repository_exports_deterministically_without_internal_state(tmp_path: Path) -> None:
    project = tmp_path / "worked"
    shutil.copytree(ASSET, project)
    site = project / ".autoform" / "site"

    exporter.export_site(project / "graph.json", site, project, git_commit=COMMIT)
    first = _files(site)
    exporter.export_site(project / "graph.json", site, project, git_commit=COMMIT)
    assert _files(site) == first

    state = json.loads((site / "data" / "state.json").read_text(encoding="utf-8"))
    assert state["publication"]["git_commit"] == COMMIT
    assert len(state["nodes"]) == 6
    assert state["coverage"]["reviewed"] == 5
    assert state["trust_frontier"] == ["Even linear combinations"]
    by_id = {node["id"]: node for node in state["nodes"]}
    assert by_id["Even linear combinations"]["proof_status"] == "kernel-evidence-recorded"

    artifact = b"\n".join(first.values()).decode("utf-8", errors="replace")
    assert "For all integers `a`, `b`, `x`, and `y`" in artifact
    assert "WorkedExample.linearCombination_isEven" in artifact
    for internal in (
        "parity:thm:linear-combination",
        "sources/source-map.md",
        "task_queue.json",
        "agents_status.json",
        "dispatch.log",
    ):
        assert internal not in artifact

    for page in site.rglob("*.html"):
        document = page.read_text(encoding="utf-8")
        for target in re.findall(r"(?:href|src)='([^']+)'", document):
            if target.startswith(("https://", "http://", "#")):
                continue
            resolved = (page.parent / target).resolve()
            assert resolved == site.resolve() or site.resolve() in resolved.parents
            assert resolved.exists(), (page, target)


def test_setup_keeps_the_worked_repository_as_soft_guidance() -> None:
    skill = (ROOT / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "skills" / "roadmap" / "SKILL.md").read_text(encoding="utf-8")
    reference = (ROOT / "skills" / "setup" / "references" / "worked-repository.md").read_text(
        encoding="utf-8"
    )
    assert "references/worked-repository.md" in skill
    assert "teaching example, not a scaffold" in skill
    assert "Roadmap owns source inspection and graph construction" in skill
    assert "Map the whole requested source" in roadmap
    assert "This reference is not a project generator" in reference
    assert "templates/github/autoform-pages.yml" in reference
    assert not (ASSET / "lakefile.toml").exists()
    assert not (ASSET / ".github" / "workflows" / "autoform-pages.yml").exists()
    assert not (ASSET / "site").exists()
