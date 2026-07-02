"""Tests for the Aristotle backend core + node-delegation entry.

These run WITHOUT the optional ``aristotlelib`` dependency and WITHOUT any
network: a tiny in-memory fake stands in for ``aristotlelib.Project`` and is
injected via the ``lib`` kwarg, so the real polling/landing logic is exercised
end-to-end against a synthetic Aristotle result.
"""

from __future__ import annotations

import asyncio
import json
import tarfile
from pathlib import Path

import pytest

from servers.aristotle.core import (
    AristotleManager,
    DEFAULT_DELEGATE_SYSTEM,
    build_node_spec,
    delegate_to_node,
    merge_payload,
)


# ---------------------------------------------------------------------------
# Fake aristotlelib (no network, no optional dependency)
# ---------------------------------------------------------------------------


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeTask:
    def __init__(self, project: "_FakeProject") -> None:
        self._project = project
        self.agent_task_id = "task-1"
        self.status = _Status("COMPLETE")
        self.output_summary = "Proved the target; lake build is green."

    async def refresh(self) -> None:  # already terminal
        return None

    async def get_events(self, limit: int = 20, newest_first: bool = True):
        return [], None


class _FakeProject:
    """Stands in for ``aristotlelib.Project``; writes a tarball on get_files."""

    def __init__(self, returned_files: dict[str, str]) -> None:
        self.project_id = "proj-aristotle"
        self._returned_files = returned_files
        self._task = _FakeTask(self)

    async def get_tasks(self, limit: int = 1, newest_first: bool = True):
        return [self._task], None

    async def ask(self, prompt: str):
        return self._task

    async def refresh(self) -> None:
        return None

    async def get_files(self, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(destination, "w:gz") as tar:
            import io

            for rel, content in self._returned_files.items():
                data = content.encode()
                info = tarfile.TarInfo(name=f"proj-aristotle/{rel}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))


class _FakeLib:
    def __init__(self, returned_files: dict[str, str]) -> None:
        self._returned_files = returned_files

        manager_lib = self

        class Project:
            @staticmethod
            async def create(prompt: str):
                return _FakeProject(manager_lib._returned_files)

            @staticmethod
            async def create_from_directory(prompt: str, project_dir: str):
                return _FakeProject(manager_lib._returned_files)

        self.Project = Project


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


def test_merge_payload_only_touches_content():
    node = {"id": "X", "tier": 2, "content": None, "mathlib_status": "missing"}
    payload = merge_payload("X", node, "informal_content/x.md")
    assert payload == {"upsert": {"X": {**node, "content": "informal_content/x.md"}}}
    # No review/verdict keys leak in.
    assert "ai" not in payload["upsert"]["X"]
    assert "verdict" not in payload["upsert"]["X"]


def test_delegate_to_node_lands_proof_and_returns_payload(tmp_path):
    gp = _write_plan(tmp_path)
    fake = _FakeLib({"MyBook/Chernoff.lean": "theorem chernoff : True := trivial\n"})
    mgr = AristotleManager(download_dir=str(tmp_path / ".cache"), lib=fake)

    result = asyncio.run(
        delegate_to_node(
            graph_path=gp,
            node_id="Chernoff bound",
            project_dir=tmp_path,
            manager=mgr,
            max_wait_seconds=5,
        )
    )

    assert result.status == "COMPLETE"
    assert result.ok
    assert result.landed_files >= 1
    # Lean file landed into the project.
    assert (tmp_path / "MyBook" / "Chernoff.lean").exists()
    # Proof recorded back into the node's prose file.
    prose = (tmp_path / "informal_content" / "chernoff-bound.md").read_text()
    assert "Proof (delegated to Aristotle)" in prose
    assert "lake build is green" in prose
    # Merge payload links content and carries no review state.
    assert result.merge_payload["upsert"]["Chernoff bound"]["content"] == (
        "informal_content/chernoff-bound.md"
    )
    assert "ai" not in result.merge_payload["upsert"]["Chernoff bound"]


def test_delegate_does_not_write_graph_or_sidecar(tmp_path):
    """The backend never writes graph.json or any review sidecar itself."""
    gp = _write_plan(tmp_path)
    before = gp.read_text()
    fake = _FakeLib({"MyBook/Chernoff.lean": "theorem c : True := trivial\n"})
    mgr = AristotleManager(download_dir=str(tmp_path / ".cache"), lib=fake)

    asyncio.run(
        delegate_to_node(
            graph_path=gp,
            node_id="Chernoff bound",
            project_dir=tmp_path,
            manager=mgr,
            max_wait_seconds=5,
        )
    )

    # graph.json is untouched (writes route through merge_node.py, not here).
    assert gp.read_text() == before
    # No review_status.json was created by the backend.
    assert not (tmp_path / "review_status.json").exists()


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
