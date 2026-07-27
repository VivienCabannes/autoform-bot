"""formalization.yaml — init, ledger rollup, human/machine split, accuracy.

The manifest module is a standalone script (scripts/formalization.py), loaded
here by path exactly the way the prover server loads it.
"""

from __future__ import annotations

import importlib.util
import multiprocessing
from pathlib import Path

import yaml

_MOD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "formalization.py"
_spec = importlib.util.spec_from_file_location("autoform_formalization", _MOD_PATH)
fz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fz)


def _read_yaml(project: Path) -> dict:
    return yaml.safe_load((project / "formalization.yaml").read_text(encoding="utf-8"))


def _autoform_entry(data: dict) -> dict:
    return next(m for m in data["automation"]["methods"]
                if m.get("framework") == "autoform")


# --------------------------------------------------------------------------- init


def test_init_creates_schema_valid_seed(tmp_path):
    path = fz.init_formalization(tmp_path, name="ConvexBodies", authors=["Jack"],
                                 license_id="Apache-2.0")
    assert path.exists()
    data = _read_yaml(tmp_path)
    # The v0.3 hard requirements, all non-blank:
    assert data["version"] == "v0.3"
    assert data["project"] == {"name": "ConvexBodies", "authors": ["Jack"],
                               "license": "Apache-2.0"}
    assert data["sources"] and data["sources"][0]["title"] and data["sources"][0]["id"]
    assert data["automation"]["methods"][0]["method"] == "agent"
    assert data["review"]["status"] == "unchecked"
    entry = _autoform_entry(data)
    assert entry["tokens"] == {"input": 0, "output": 0, "runs": 0}


def test_init_refuses_overwrite_without_force(tmp_path):
    fz.init_formalization(tmp_path, name="X")
    (tmp_path / "formalization.yaml").write_text("version: v0.3\nproject: {name: HandEdited, authors: [a], license: MIT}\n")
    try:
        fz.init_formalization(tmp_path, name="Y")
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass
    # force overwrites
    fz.init_formalization(tmp_path, name="Y", force=True)
    assert _read_yaml(tmp_path)["project"]["name"] == "Y"


def test_init_backfills_from_existing_ledger(tmp_path):
    """A late opt-in rolls up runs recorded before the manifest existed."""
    fz.record_run(tmp_path, {"node": "A", "backend": "claude", "model": "opus",
                             "status": "proved", "wall_seconds": 90, "billing": "subscription",
                             "usage": {"worker": {"input_tokens": 100, "output_tokens": 50,
                                                  "cost_usd": 0.5}, "judge": {}}})
    fz.init_formalization(tmp_path, name="X")
    entry = _autoform_entry(_read_yaml(tmp_path))
    assert entry["tokens"] == {"input": 100, "output": 50, "runs": 1}
    assert entry["models"] == ["opus"]
    assert entry["cost"]["wall_time"] == "1m"


# ------------------------------------------------------------------------- ledger


def test_rollup_math_across_backends(tmp_path):
    fz.init_formalization(tmp_path, name="X")
    fz.record_run(tmp_path, {"node": "A", "backend": "claude", "model": "opus",
                             "status": "proved", "wall_seconds": 3600, "billing": "subscription",
                             "usage": {"worker": {"input_tokens": 1000, "output_tokens": 400,
                                                  "cost_usd": 1.25},
                                       "judge": {"input_tokens": 10, "output_tokens": 5,
                                                 "cost_usd": 0.01}}})
    fz.record_run(tmp_path, {"node": "B", "backend": "aristotle", "model": "",
                             "status": "failed", "wall_seconds": 4000, "billing": "external-compute",
                             "usage": {"worker": {}, "judge": {}}})
    fz.record_run(tmp_path, {"node": "C", "backend": "avocado", "model": "muse-spark-1.1",
                             "status": "proved", "wall_seconds": 30, "billing": "api",
                             "usage": {"worker": {"input_tokens": 200, "output_tokens": 100},
                                       "judge": {}}})
    assert fz.update_formalization(tmp_path) is not None
    entry = _autoform_entry(_read_yaml(tmp_path))
    assert entry["tokens"] == {"input": 1210, "output": 505, "runs": 3}
    assert entry["models"] == ["Aristotle", "muse-spark-1.1", "opus"]
    assert entry["cost"]["wall_time"] == "2h 7m"          # 7630s
    # Notional sums SUBSCRIPTION entries only (claude: 1.25 + 0.01); the
    # aristotle run is externally billed, never "subscription".
    assert "subscription-based usage (1 runs" in entry["cost"]["spend_usd"]
    assert "notional API-equivalent $1.26" in entry["cost"]["spend_usd"]
    assert "API-billed runs: 1" in entry["cost"]["spend_usd"]
    assert "externally-billed runs: 1" in entry["cost"]["spend_usd"]


def test_torn_ledger_line_is_skipped(tmp_path):
    fz.record_run(tmp_path, {"node": "A", "usage": {"worker": {"input_tokens": 7}}})
    with fz.ledger_path_for(tmp_path).open("a") as fh:
        fh.write('{"node": "torn...')  # crash mid-append
    entries = fz.read_ledger(tmp_path)
    assert len(entries) == 1 and entries[0]["node"] == "A"


def _record_and_refresh(project: str, index: int) -> None:
    fz.record_run(project, {
        "node": f"N{index}",
        "backend": "codex",
        "model": "pilot",
        "billing": "external",
        "wall_seconds": 1,
        "usage": {
            "worker": {"input_tokens": 1, "output_tokens": 1},
            "judge": {},
        },
    })
    fz.update_formalization(project)


def test_concurrent_ledger_writers_keep_every_run_and_latest_rollup(tmp_path):
    fz.init_formalization(tmp_path, name="Concurrent")
    ctx = multiprocessing.get_context("fork")
    processes = [
        ctx.Process(target=_record_and_refresh, args=(str(tmp_path), index))
        for index in range(24)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    entries = fz.read_ledger(tmp_path)
    assert len(entries) == 24
    assert {entry["node"] for entry in entries} == {f"N{i}" for i in range(24)}
    method = _autoform_entry(_read_yaml(tmp_path))
    assert method["tokens"] == {"input": 24, "output": 24, "runs": 24}


def test_wall_time_formatting():
    assert fz._format_wall_time(0) == ""
    assert fz._format_wall_time(30) == "<1m"
    assert fz._format_wall_time(150) == "2m"
    assert fz._format_wall_time(3 * 3600 + 20 * 60) == "3h 20m"
    assert fz._format_wall_time(102_490) == "1d 4h"        # GeometricAnalysis's real total


# ----------------------------------------------------------- human/machine split


def test_human_fields_and_unknown_keys_round_trip(tmp_path):
    fz.init_formalization(tmp_path, name="X")
    data = _read_yaml(tmp_path)
    data["sources"] = [{"title": "Lee, Smooth Manifolds", "authors": ["John M. Lee"],
                        "id": "doi:10.1007/978-1-4419-9982-5", "type": "textbook"}]
    data["review"] = {"status": "self-assessed", "reviewers": ["Jack"], "notes": "ch1 ok"}
    data["automation"]["methods"].append({"method": "autonomous", "framework": "Marathon",
                                          "models": ["Aristotle"]})
    data["custom_top_level_key"] = {"anything": True}
    (tmp_path / "formalization.yaml").write_text(
        yaml.dump(data, sort_keys=False), encoding="utf-8")

    fz.record_run(tmp_path, {"node": "A", "backend": "claude", "model": "opus",
                             "status": "proved", "wall_seconds": 60,
                             "usage": {"worker": {"input_tokens": 5, "output_tokens": 5}}})
    assert fz.update_formalization(tmp_path) is not None
    after = _read_yaml(tmp_path)
    # Human-owned content survived verbatim:
    assert after["sources"][0]["title"] == "Lee, Smooth Manifolds"
    assert after["review"]["reviewers"] == ["Jack"]
    assert after["custom_top_level_key"] == {"anything": True}
    marathon = [m for m in after["automation"]["methods"] if m.get("framework") == "Marathon"]
    assert marathon and marathon[0]["models"] == ["Aristotle"]
    # …while the autoform entry moved:
    assert _autoform_entry(after)["tokens"]["runs"] == 1


def test_update_is_noop_when_nothing_changed(tmp_path):
    fz.init_formalization(tmp_path, name="X")
    fz.record_run(tmp_path, {"node": "A", "backend": "claude", "model": "opus",
                             "usage": {"worker": {"input_tokens": 1, "output_tokens": 1}}})
    assert fz.update_formalization(tmp_path) is not None       # real change
    before = (tmp_path / "formalization.yaml").read_text()
    assert fz.update_formalization(tmp_path) is None           # timestamp-only → skip
    assert (tmp_path / "formalization.yaml").read_text() == before
    # The stamp never accumulates: exactly one stamp line.
    assert before.count("_auto: last updated by autoform") == 1


def test_update_without_manifest_is_noop(tmp_path):
    fz.record_run(tmp_path, {"node": "A"})
    assert fz.update_formalization(tmp_path) is None
    assert not (tmp_path / "formalization.yaml").exists()      # opt-in stays opt-in


# ------------------------------------------------------------------------ sorries


def test_sorry_counting_split_and_comments(tmp_path):
    (tmp_path / "A.lean").write_text(
        "theorem foo : True := by sorry\n"
        "def bar : Nat := sorry\n"
        "-- a commented sorry does not count\n"
        "/- nor a sorry in a block comment -/\n"
        "lemma baz : 1 = 1 := by sorry\n",
        encoding="utf-8")
    total, in_defs = fz.count_sorries(tmp_path)
    assert (total, in_defs) == (3, 1)


def test_sorry_counts_flow_into_status(tmp_path):
    fz.init_formalization(tmp_path, name="X")
    (tmp_path / "A.lean").write_text("theorem t : True := sorry\n", encoding="utf-8")
    fz.update_formalization(tmp_path)
    data = _read_yaml(tmp_path)
    assert data["status"]["sorry_count"] == 1
    assert data["status"]["sorry_in_definitions"] == 0


# ------------------------------------------------- server wiring (_record_usage)


def test_server_record_usage_appends_ledger_and_refreshes_yaml(tmp_path):
    from servers.prover.base import ProofResult
    from servers.prover.server import _record_usage

    fz.init_formalization(tmp_path, name="X")
    result = ProofResult(status="proved", backend="claude", meta={
        "model": "opus",
        "usage": {"worker": {"input_tokens": 42, "output_tokens": 7, "cost_usd": 0.1},
                  "judge": {"input_tokens": 3, "output_tokens": 1, "cost_usd": 0.01,
                            "calls": 1},
                  "wall_seconds": 120.5},
    })
    _record_usage(str(tmp_path), "Chernoff bound", "claude", result)

    entries = fz.read_ledger(tmp_path)
    assert len(entries) == 1
    assert entries[0]["node"] == "Chernoff bound"
    assert entries[0]["billing"] == "subscription"
    assert entries[0]["usage"]["worker"]["input_tokens"] == 42

    entry = _autoform_entry(_read_yaml(tmp_path))
    assert entry["tokens"] == {"input": 45, "output": 8, "runs": 1}
    assert entry["cost"]["wall_time"] == "2m"


def test_server_record_usage_never_raises(tmp_path):
    from servers.prover.base import ProofResult
    from servers.prover.server import _record_usage

    # A nonexistent project dir must not break the proof result path.
    _record_usage(str(tmp_path / "nope" / "deeper"), "N", "claude",
                  ProofResult(status="failed"))


def test_sorry_scan_survives_comment_pathologies(tmp_path):
    """Line comments mentioning /- must not swallow real code (undercount);
    nested block comments and string literals must not add phantom sorries."""
    (tmp_path / "A.lean").write_text(
        "-- see the /- old attempt\n"
        "theorem real : True := by sorry\n"
        "-- end of notes -/\n"
        "/- outer /- nested -/ still comment: sorry -/\n"
        'def msg : String := "please do not sorry here"\n',
        encoding="utf-8")
    assert fz.count_sorries(tmp_path) == (1, 0)


def test_update_tolerates_hand_typed_scalars(tmp_path):
    """A human replacing machine-adjacent mappings with scalars gets a working
    refresh, not a traceback."""
    fz.init_formalization(tmp_path, name="X")
    (tmp_path / "formalization.yaml").write_text(
        "version: v0.3\n"
        "project: {name: X, authors: [a], license: MIT}\n"
        "sources: [{title: T, authors: [a], id: I}]\n"
        "automation: autoform\n"          # scalar!
        "status: in progress\n"           # scalar!
        "review: {status: unchecked}\n",
        encoding="utf-8")
    fz.record_run(tmp_path, {"node": "A", "backend": "claude", "model": "opus",
                             "billing": "subscription",
                             "usage": {"worker": {"input_tokens": 1, "output_tokens": 1}}})
    assert fz.update_formalization(tmp_path) is not None
    data = _read_yaml(tmp_path)
    assert data["status"]["sorry_count"] == 0
    assert _autoform_entry(data)["tokens"]["runs"] == 1
