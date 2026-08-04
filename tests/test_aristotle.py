"""Tests for the shared Aristotle manager helpers used by the prover adapter."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from servers.aristotle.core import (
    DEFAULT_DELEGATE_SYSTEM,
    _safe_extract,
    build_node_spec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_plan(tmp_path: Path) -> Path:
    """A minimal v2 plan with one tier-2 target node that has prose."""
    (tmp_path / "informal_content").mkdir(parents=True, exist_ok=True)
    (tmp_path / "informal_content" / "chernoff-bound.md").write_text(
        "# Chernoff bound\n\nFor every a, P(X >= a) <= inf_t e^{-ta} M_X(t).\n",
        encoding="utf-8",
    )
    graph = {
        "version": 2,
        "metadata": {"sources": []},
        "nodes": {
            "Chernoff bound": {
                "id": "Chernoff bound",
                "tier": 2,
                "parent": "Concentration inequalities",
                "kind": "theorem",
                "depends_on": ["Markov's inequality"],
                "mathlib_status": "partial",
                "mathlib_declarations": ["ProbabilityTheory.measure_ge_le_exp_mul_mgf"],
                "mathlib_file": "Mathlib/Probability/Moments/Basic.lean",
                "source_refs": [{"file": "sources/hds.pdf", "location": "Ch 1, Thm 1.6"}],
                "content": "informal_content/chernoff-bound.md",
            }
        },
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    return gp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_node_spec_includes_statement_and_refs(tmp_path):
    gp = _write_plan(tmp_path)
    spec = build_node_spec(gp, "Chernoff bound", project_dir=tmp_path)
    assert "Chernoff bound" in spec
    assert "ProbabilityTheory.measure_ge_le_exp_mul_mgf" in spec
    assert "Markov's inequality" in spec  # depends_on surfaced
    assert "Ch 1, Thm 1.6" in spec  # source_ref surfaced
    assert "inf_t" in spec  # prose statement injected


def test_build_node_spec_unknown_node_raises(tmp_path):
    gp = _write_plan(tmp_path)
    with pytest.raises(KeyError):
        build_node_spec(gp, "Nope", project_dir=tmp_path)


def test_build_node_spec_rejects_content_outside_project(tmp_path):
    gp = _write_plan(tmp_path)
    graph = json.loads(gp.read_text(encoding="utf-8"))
    graph["nodes"]["Chernoff bound"]["content"] = "../../outside.md"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes project root"):
        build_node_spec(gp, "Chernoff bound", project_dir=tmp_path)


def test_safe_extract_rejects_tar_traversal_and_links(tmp_path):
    for name, configure in (
        ("../outside.txt", lambda info: None),
        ("project/link", lambda info: (setattr(info, "type", tarfile.SYMTYPE),
                                        setattr(info, "linkname", "../../outside.txt"))),
    ):
        archive = tmp_path / ("traversal.tar" if name.startswith("..") else "link.tar")
        with tarfile.open(archive, "w") as tar:
            info = tarfile.TarInfo(name)
            data = b"bad"
            info.size = len(data)
            configure(info)
            tar.addfile(info, io.BytesIO(data) if info.isfile() else None)
        with pytest.raises(ValueError, match="tar member|unsafe tar"):
            _safe_extract(archive, tmp_path / "extract")
    assert not (tmp_path / "outside.txt").exists()


def test_default_system_prompt_forbids_cheating():
    assert "sorry" in DEFAULT_DELEGATE_SYSTEM
    assert "axiom" in DEFAULT_DELEGATE_SYSTEM


def test_overlay_lands_only_lean_files_and_protects_build_config(tmp_path):
    """Aristotle's returned lakefile/toolchain (and any non-.lean file) must never
    overwrite the user's project — only .lean files are overlaid."""
    from servers.aristotle.core import _overlay_project

    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text("name = 'mine'\n", encoding="utf-8")
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.10.0\n", encoding="utf-8")

    returned = tmp_path / "returned"
    (returned / "MyBook").mkdir(parents=True)
    (returned / "MyBook" / "Thm.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")
    (returned / "lakefile.toml").write_text("name = 'aristotle-repin'\n", encoding="utf-8")
    (returned / "lakefile.lean").write_text("-- evil\n", encoding="utf-8")
    (returned / "lean-toolchain").write_text("other-toolchain\n", encoding="utf-8")
    (returned / "README.md").write_text("junk\n", encoding="utf-8")

    copied = _overlay_project(returned, project)

    assert copied == 1
    assert (project / "MyBook" / "Thm.lean").exists()
    assert (project / "lakefile.toml").read_text() == "name = 'mine'\n"      # untouched
    assert (project / "lean-toolchain").read_text() == "leanprover/lean4:v4.10.0\n"
    assert not (project / "lakefile.lean").exists()
    assert not (project / "README.md").exists()
