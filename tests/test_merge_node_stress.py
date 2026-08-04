"""Concurrency and fail-closed tests for the graph's single writer."""

from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts"))

import merge_node as mn  # noqa: E402


def _graph(tmp_path: Path, nodes: dict | None = None) -> Path:
    path = tmp_path / "graph.json"
    path.write_text(
        json.dumps({"version": 2, "metadata": {}, "nodes": nodes or {}}),
        encoding="utf-8",
    )
    return path


def _node(node_id: str, **extra) -> dict:
    return {
        "id": node_id,
        "tier": 1,
        "parent": None,
        "depends_on": [],
        **extra,
    }


def _merge_process(graph: str, payload: str) -> None:
    rc = mn.main([graph, "--payload", payload])
    if rc:
        raise SystemExit(rc)


def test_cross_process_merge_stress_has_no_lost_nodes(tmp_path: Path):
    graph = _graph(tmp_path)
    payloads = []
    for index in range(32):
        payload = tmp_path / f"payload-{index}.json"
        node_id = f"node-{index}"
        payload.write_text(json.dumps({"upsert": {node_id: _node(node_id)}}))
        payloads.append(payload)

    ctx = multiprocessing.get_context("fork")
    processes = [
        ctx.Process(target=_merge_process, args=(str(graph), str(payload)))
        for payload in payloads
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0

    nodes = json.loads(graph.read_text(encoding="utf-8"))["nodes"]
    assert set(nodes) == {f"node-{index}" for index in range(32)}
    assert all(node["id"] == node_id for node_id, node in nodes.items())


def test_unresolved_dependency_is_rejected_without_changing_graph(tmp_path: Path):
    graph = _graph(tmp_path)
    before = graph.read_bytes()
    with pytest.raises(ValueError, match="absent dependencies"):
        mn.merge(
            str(graph),
            {"upsert": {"dependent": _node("dependent", depends_on=["prerequisite"])}},
        )
    assert graph.read_bytes() == before


def test_unresolved_parent_is_rejected_without_changing_graph(tmp_path: Path):
    graph = _graph(tmp_path)
    before = graph.read_bytes()
    with pytest.raises(ValueError, match="absent parent"):
        mn.merge(
            str(graph),
            {"upsert": {"child": _node("child", tier=2, parent="parent")}},
        )
    assert graph.read_bytes() == before


def test_prerequisite_and_dependent_can_land_in_one_atomic_payload(tmp_path: Path):
    graph = _graph(tmp_path)
    result = mn.merge(
        str(graph),
        {
            "upsert": {
                "prerequisite": _node("prerequisite"),
                "dependent": _node("dependent", depends_on=["prerequisite"]),
            }
        },
    )
    assert result["upserted"] == 2
    nodes = json.loads(graph.read_text())["nodes"]
    assert nodes["dependent"]["depends_on"] == ["prerequisite"]


def test_metadata_patch_records_confirmed_scope_with_node_merge(tmp_path: Path):
    graph = _graph(tmp_path)
    result = mn.merge(
        str(graph),
        {
            "metadata": {"confirmed_scope": ["scalar.weighted_hoeffding"]},
            "upsert": {"weighted": _node("weighted")},
        },
    )
    saved = json.loads(graph.read_text())
    assert saved["metadata"]["confirmed_scope"] == ["scalar.weighted_hoeffding"]
    assert result["metadata_updated"] == 1


def test_delete_strips_dependencies_and_explicitly_orphans_children(tmp_path: Path):
    graph = _graph(
        tmp_path,
        {
            "parent": _node("parent"),
            "child": _node("child", tier=2, parent="parent"),
            "dependent": _node("dependent", depends_on=["parent"]),
        },
    )
    result = mn.merge(str(graph), {"delete": ["parent"]})
    nodes = json.loads(graph.read_text())["nodes"]
    assert nodes["child"]["parent"] is None
    assert nodes["dependent"]["depends_on"] == []
    assert result["orphaned_children"] == [("child", "parent")]
    assert result["stripped_edges"] == [("dependent", "parent")]


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"upsert": ["bad"]}, "record must be an object"),
        ({"upsert": {"x": "bad"}}, "must be an object"),
        ({"delete": "x"}, "must be a list"),
        ({"delete": ["x", "x"]}, "duplicate"),
    ],
)
def test_malformed_payload_is_rejected_without_write(
    tmp_path: Path, payload: dict, message: str
):
    graph = _graph(tmp_path)
    before = graph.read_bytes()
    with pytest.raises(ValueError, match=message):
        mn.merge(str(graph), payload)
    assert graph.read_bytes() == before


def test_cli_reports_rejection_without_traceback_or_write(tmp_path: Path, capsys):
    graph = _graph(tmp_path)
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {"upsert": {"dependent": _node("dependent", depends_on=["missing"])}}
        )
    )
    before = graph.read_bytes()
    assert mn.main([str(graph), "--payload", str(payload)]) == 2
    assert "merge rejected" in capsys.readouterr().err
    assert graph.read_bytes() == before
