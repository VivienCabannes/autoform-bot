"""Spend-governor tests — deterministic pacing over the project prover ledger.

Covers scripts/spend_governor.py: fail-closed budget loading, the rolling-window ledger fold, check()'s
global and per-backend caps, main()'s exit codes, and the survey integration
that turns an exhausted budget into SUPPRESSED prove candidates.

Everything is offline and under tmp_path: no network, no real ledger, and the
survey harness reuses tests/test_worker_survey.py's canned GitHost runner.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "review_ui"))

import spend_governor as sg   # noqa: E402

from tests.test_worker_survey import make_cfg, make_runner, prove_map, run_collect   # noqa: E402

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0


# ---------------------------------------------------------------------------
# synthetic project builder
# ---------------------------------------------------------------------------

def make_project(tmp_path, name="proj") -> Path:
    proj = tmp_path / name
    (proj / ".autoform").mkdir(parents=True, exist_ok=True)
    return proj


def write_budget(project: Path, data) -> Path:
    """``data`` is a JSON-able object, or a raw string written verbatim."""
    path = Path(project) / ".autoform" / "budget.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def write_ledger(project: Path, lines) -> Path:
    """``lines`` are dicts (one JSONL record each) or raw strings (torn lines)."""
    path = Path(project) / ".autoform" / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)
    path.write_text(body, encoding="utf-8")
    return path


def run_entry(ts, status="proved", backend="claude", wall=10.0, **extra):
    rec = {"ts": ts, "node": "n1", "backend": backend, "status": status, "wall_seconds": wall}
    rec.update(extra)
    return rec


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# load_budget
# ---------------------------------------------------------------------------

def test_no_budget_file_is_unlimited(tmp_path):
    proj = make_project(tmp_path)
    assert sg.budget_path(proj) == proj / ".autoform" / "budget.json"
    assert sg.load_budget(proj) == sg.Budget()
    assert not sg.load_budget(proj).enabled

    result = sg.check(proj, "claude", now=NOW)
    assert result["allowed"] is True
    assert result["paced"] is False
    assert result["reason"] == "no budget configured"
    assert result["usage"] == {}


@pytest.mark.parametrize("raw", [
    "{not json at all",
    "[1, 2, 3]",
    '"a budget, allegedly"',
    "",
    "null",
])
def test_malformed_budget_fails_closed(tmp_path, raw):
    proj = make_project(tmp_path)
    write_budget(proj, raw)
    # A ledger that would blow any real cap — irrelevant, since nothing is configured.
    write_ledger(proj, [run_entry(NOW, wall=9999.0) for _ in range(50)])
    with pytest.raises(sg.BudgetConfigError):
        sg.load_budget(proj)
    result = sg.check(proj, "claude", now=NOW)
    assert result["allowed"] is False and result["paced"] is True
    assert result["configuration_error"] is True


def test_budget_with_window_but_no_limits_is_disabled(tmp_path):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5})
    budget = sg.load_budget(proj)
    assert budget.window_hours == 5.0 and not budget.enabled
    write_ledger(proj, [run_entry(NOW) for _ in range(9)])
    assert sg.check(proj, "claude", now=NOW)["allowed"] is True


def test_budget_enabled_matrix(tmp_path):
    proj = make_project(tmp_path)

    def enabled(data):
        write_budget(proj, data)
        return sg.load_budget(proj).enabled

    assert enabled({"window_hours": 5, "max_runs": 1})
    assert enabled({"window_hours": 5, "max_wall_seconds": 1})
    assert enabled({"window_hours": 5, "backends": {"aristotle": {"max_runs": 1}}})
    assert not enabled({"window_hours": 5, "max_runs": 0, "max_wall_seconds": 0, "backends": {}})
    # A window is mandatory: limits without one configure nothing.
    assert not enabled({"max_runs": 1, "max_wall_seconds": 60})
    # A non-dict `backends` is an invalid spending policy.
    write_budget(proj, {"window_hours": 5, "max_runs": 2, "backends": ["aristotle"]})
    with pytest.raises(sg.BudgetConfigError):
        sg.load_budget(proj)


# ---------------------------------------------------------------------------
# window_usage
# ---------------------------------------------------------------------------

def test_window_usage_missing_ledger_is_zero(tmp_path):
    proj = make_project(tmp_path)
    assert sg.window_usage(proj, 5, NOW) == {"runs": 0, "wall_seconds": 0.0, "backends": {}}


def test_window_usage_counts_only_spending_statuses(tmp_path):
    proj = make_project(tmp_path)
    write_ledger(proj, [
        run_entry(NOW, status="proved", wall=1.0),
        run_entry(NOW, status="failed", wall=2.0),
        run_entry(NOW, status="skipped", wall=100.0),
        run_entry(NOW, status="error", wall=100.0),
        run_entry(NOW, status="cached", wall=100.0),
        run_entry(NOW, status="", wall=100.0),
        {"ts": NOW, "backend": "claude", "wall_seconds": 100.0},          # no status at all
    ])
    usage = sg.window_usage(proj, 5, NOW)
    assert usage["runs"] == 2
    assert usage["wall_seconds"] == 3.0


def test_window_usage_rolling_window_boundary(tmp_path):
    """cutoff = now - window; an entry exactly at the cutoff still counts."""
    proj = make_project(tmp_path)
    cutoff = NOW - 5 * HOUR
    write_ledger(proj, [
        run_entry(cutoff - 1.0, wall=1.0),      # just outside — aged out
        run_entry(cutoff, wall=2.0),            # exactly at the edge — inside
        run_entry(cutoff + 1.0, wall=4.0),      # just inside
        run_entry(NOW, wall=8.0),               # now
    ])
    usage = sg.window_usage(proj, 5, NOW)
    assert usage["runs"] == 3
    assert usage["wall_seconds"] == 14.0
    # A wider window pulls the aged-out entry back in.
    assert sg.window_usage(proj, 6, NOW)["runs"] == 4
    # A narrower one drops everything but `now`.
    assert sg.window_usage(proj, 0.5, NOW)["runs"] == 1


def test_window_usage_parses_iso_and_epoch_timestamps(tmp_path):
    proj = make_project(tmp_path)
    write_ledger(proj, [
        run_entry(iso(NOW - HOUR), wall=1.0),                       # ISO-8601 with a Z
        run_entry(NOW - 2 * HOUR, wall=2.0),                        # float epoch
        run_entry(int(NOW - 3 * HOUR), wall=4.0),                   # int epoch
        run_entry("2026-08-04T09:00:00+00:00", wall=8.0),           # explicit offset
        run_entry("2026-08-04T08:00:00", wall=16.0),                # naive → assumed UTC
        {"at": iso(NOW), "backend": "claude", "status": "proved", "wall_seconds": 32.0},
    ])
    usage = sg.window_usage(proj, 5, NOW)
    assert usage["runs"] == 6
    assert usage["wall_seconds"] == 63.0

    # Unparseable / missing timestamps are dropped, never counted at "now".
    write_ledger(proj, [
        run_entry("not a timestamp", wall=1.0),
        run_entry("", wall=1.0),
        run_entry(None, wall=1.0),
        {"backend": "claude", "status": "proved", "wall_seconds": 1.0},
    ])
    assert sg.window_usage(proj, 5, NOW) == {"runs": 0, "wall_seconds": 0.0, "backends": {}}


def test_window_usage_tolerates_a_torn_ledger(tmp_path):
    proj = make_project(tmp_path)
    write_ledger(proj, [
        run_entry(NOW, wall=1.0),
        "",                                        # blank line
        "   ",                                     # whitespace-only line
        '"a bare string"',                         # valid JSON, not a dict
        "[1, 2, 3]",                               # valid JSON, not a dict
        "null",
        run_entry(NOW, wall=2.0),
        '{"ts": ' + repr(NOW) + ', "status": "proved", "wall_s',   # torn trailing write
    ])
    usage = sg.window_usage(proj, 5, NOW)
    assert usage["runs"] == 2 and usage["wall_seconds"] == 3.0


def test_window_usage_aggregates_per_backend(tmp_path):
    proj = make_project(tmp_path)
    write_ledger(proj, [
        run_entry(NOW, backend="claude", wall=1.0),
        run_entry(NOW, backend="claude", wall=2.0, status="failed"),
        run_entry(NOW, backend="aristotle", wall=4.0),
        run_entry(NOW - 6 * HOUR, backend="aristotle", wall=999.0),    # outside the window
        {"ts": NOW, "status": "proved", "wall_seconds": 8.0},          # no backend → "unknown"
        run_entry(NOW, backend="codex", wall="not a number"),          # bad wall → 0.0
    ])
    usage = sg.window_usage(proj, 5, NOW)
    assert usage["runs"] == 5
    assert usage["wall_seconds"] == 15.0
    assert usage["backends"] == {
        "claude": {"runs": 2, "wall_seconds": 3.0},
        "aristotle": {"runs": 1, "wall_seconds": 4.0},
        "unknown": {"runs": 1, "wall_seconds": 8.0},
        "codex": {"runs": 1, "wall_seconds": 0.0},
    }


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def test_check_max_runs_cap(tmp_path):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "max_runs": 3})

    write_ledger(proj, [run_entry(NOW, wall=1.0) for _ in range(2)])
    ok = sg.check(proj, "claude", now=NOW)
    assert ok["allowed"] is True
    assert ok["reason"] == "within budget (2 runs in the 5h window)"
    assert ok["usage"]["runs"] == 2

    write_ledger(proj, [run_entry(NOW, wall=1.0) for _ in range(3)])
    paced = sg.check(proj, "claude", now=NOW)
    assert paced["allowed"] is False and paced["paced"] is True
    assert paced["reason"] == "3/3 prover runs used in the 5h window"
    assert paced["usage"]["runs"] == 3


def test_check_max_wall_seconds_cap(tmp_path):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "max_wall_seconds": 100})

    write_ledger(proj, [run_entry(NOW, wall=99.0)])
    assert sg.check(proj, "claude", now=NOW)["allowed"] is True

    write_ledger(proj, [run_entry(NOW, wall=60.0), run_entry(NOW, wall=40.0)])
    paced = sg.check(proj, "claude", now=NOW)
    assert paced["allowed"] is False and paced["paced"] is True
    assert paced["reason"] == "100/100 prover seconds used in the 5h window"


def test_check_per_backend_caps_bind_only_the_named_backend(tmp_path):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "backends": {
        "aristotle": {"max_runs": 2},
        "codex": {"max_wall_seconds": 30},
    }})
    write_ledger(proj, [
        run_entry(NOW, backend="aristotle", wall=1.0),
        run_entry(NOW, backend="aristotle", wall=1.0),
        run_entry(NOW, backend="codex", wall=30.0),
        run_entry(NOW, backend="claude", wall=500.0),
    ])

    aristotle = sg.check(proj, "aristotle", now=NOW)
    assert aristotle["allowed"] is False
    assert aristotle["reason"] == "backend aristotle: 2/2 runs used in the 5h window"

    codex = sg.check(proj, "codex", now=NOW)
    assert codex["allowed"] is False
    assert codex["reason"] == "backend codex: 30/30 seconds used in the 5h window"

    # An uncapped backend keeps running in the very same state — per-backend caps
    # are not a global brake.
    assert sg.check(proj, "claude", now=NOW)["allowed"] is True
    assert sg.check(proj, "max", now=NOW)["allowed"] is True


def test_max_backend_name_uses_claude_ledger_and_limits(tmp_path):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "backends": {"max": {"max_runs": 1}}})
    write_ledger(proj, [run_entry(NOW, backend="claude")])
    result = sg.check(proj, "max", now=NOW)
    assert result["allowed"] is False
    assert "backend claude: 1/1" in result["reason"]


def test_reservation_atomically_consumes_last_run_slot(tmp_path):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "max_runs": 1})
    first = sg.reserve(proj, "claude", now=NOW)
    second = sg.reserve(proj, "claude", now=NOW)
    assert first["allowed"] is True and first["reservation_id"]
    assert second["allowed"] is False and second["reservation_id"] is None
    sg.release(proj, first["reservation_id"])
    assert sg.reserve(proj, "claude", now=NOW)["allowed"] is True


def test_check_global_cap_outranks_an_unspent_backend_cap(tmp_path):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "max_runs": 2,
                        "backends": {"aristotle": {"max_runs": 99}}})
    write_ledger(proj, [run_entry(NOW, backend="claude"), run_entry(NOW, backend="claude")])
    result = sg.check(proj, "aristotle", now=NOW)
    assert result["allowed"] is False
    assert result["reason"] == "2/2 prover runs used in the 5h window"


def test_check_recovers_by_itself_as_the_window_rolls(tmp_path):
    """The rolling lookback is the whole autonomy story: nobody resets anything."""
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "max_runs": 2})
    write_ledger(proj, [
        run_entry(NOW - 4 * HOUR, wall=1.0),
        run_entry(NOW - 3 * HOUR, wall=1.0),
    ])
    assert sg.check(proj, "claude", now=NOW)["allowed"] is False
    # One entry has aged out an hour later; the fleet resumes without a human.
    assert sg.check(proj, "claude", now=NOW + 1.5 * HOUR)["allowed"] is True
    assert sg.check(proj, "claude", now=NOW + 3 * HOUR)["usage"]["runs"] == 0


def test_check_ledger_missing_under_a_live_budget_is_allowed(tmp_path):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "max_runs": 1})
    result = sg.check(proj, "claude", now=NOW)
    assert result["allowed"] is True
    assert result["usage"] == {"runs": 0, "wall_seconds": 0.0, "backends": {}}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def test_main_exits_0_when_allowed(tmp_path, capsys):
    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "max_runs": 3})
    assert sg.main([str(proj), "--backend", "claude"]) == 0
    assert capsys.readouterr().out.startswith("ALLOW ")


def test_main_exits_75_when_paced(tmp_path, capsys):
    import time

    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "max_runs": 1})
    write_ledger(proj, [run_entry(time.time(), wall=1.0)])
    assert sg.main([str(proj)]) == 75
    assert capsys.readouterr().out.startswith("PACE  1/1 prover runs")


def test_main_json_is_parseable(tmp_path, capsys):
    import time

    proj = make_project(tmp_path)
    write_budget(proj, {"window_hours": 5, "backends": {"aristotle": {"max_runs": 1}}})
    write_ledger(proj, [run_entry(time.time(), backend="aristotle", wall=7.0)])

    assert sg.main([str(proj), "--backend", "aristotle", "--json"]) == 75
    payload = json.loads(capsys.readouterr().out)
    assert payload["allowed"] is False and payload["paced"] is True
    assert payload["usage"]["backends"]["aristotle"] == {"runs": 1, "wall_seconds": 7.0}
    assert "backend aristotle" in payload["reason"]

    assert sg.main([str(proj), "--backend", "claude", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["allowed"] is True


# ---------------------------------------------------------------------------
# survey integration
# ---------------------------------------------------------------------------

def test_survey_prove_is_actionable_without_a_budget(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    s = run_collect(cfg, make_runner())
    assert set(prove_map(s)) == {"no-lean", "sorried", "rejected"}
    assert prove_map(s, actionable=False) == {}
    assert not [note for note in s.notes if "paced" in note]


def test_survey_suppresses_prove_when_the_budget_is_spent(tmp_path, monkeypatch):
    import time

    cfg = make_cfg(tmp_path, monkeypatch)
    # collect() defaults to the "max" backend; a global cap paces it regardless.
    write_budget(cfg.lean_root, {"window_hours": 5, "max_runs": 2})
    write_ledger(cfg.lean_root, [run_entry(time.time(), backend="max", wall=60.0) for _ in range(2)])

    s = run_collect(cfg, make_runner())
    assert s.actionable("prove") == []
    suppressed = prove_map(s, actionable=False)
    assert set(suppressed) == {"no-lean", "sorried", "rejected"}
    assert all("paced" in reason for reason in suppressed.values())
    assert all("2/2 prover runs used in the 5h window" in reason for reason in suppressed.values())
    assert [note for note in s.notes if note.startswith("prove paced: ")]

    # Pacing bounds resources only — the other stages are untouched.
    assert s.suppressed["review"] == [] and s.suppressed["progress"] == []


def test_survey_prove_returns_once_the_ledger_ages_out(tmp_path, monkeypatch):
    import time

    cfg = make_cfg(tmp_path, monkeypatch)
    write_budget(cfg.lean_root, {"window_hours": 1, "max_runs": 1})
    write_ledger(cfg.lean_root, [run_entry(time.time() - 6 * HOUR, backend="max", wall=60.0)])
    s = run_collect(cfg, make_runner())
    assert set(prove_map(s)) == {"no-lean", "sorried", "rejected"}
    assert not [note for note in s.notes if "paced" in note]


# ---------------------------------------------------------------------------
# fail-closed regression: a typo in a budget value must pace work
# ---------------------------------------------------------------------------

# Existing but invalid policies must suppress paid work with a configuration
# error rather than silently turning pacing off.
TYPOED_BUDGETS = [
    {"window_hours": "5h", "max_runs": 3},
    {"window_hours": 5, "max_runs": "forty"},
    {"window_hours": 5, "max_wall_seconds": "4h"},
    {"window_hours": [5], "max_runs": 3},
    {"window_hours": 5, "max_runs": 3, "backends": {"claude": "max_runs=2"}},
]


def test_typoed_budget_values_fail_closed(tmp_path):
    for index, data in enumerate(TYPOED_BUDGETS):
        proj = make_project(tmp_path, f"proj{index}")
        write_budget(proj, data)
        result = sg.check(proj, "claude", now=NOW)
        assert result["allowed"] is False
        assert result["configuration_error"] is True
