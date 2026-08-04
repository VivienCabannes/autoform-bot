"""Tests for autoform_worker.scoreboard and autoform_worker.counters."""

from __future__ import annotations

import json

from autoform_worker import counters, scoreboard
from autoform_worker.constants import (
    INPROGRESS_MARK_RE,
    MAX_INFRA_REFUNDS,
    META_MARK_RE,
    SCOREBOARD_MARK,
)


# -- target marker -----------------------------------------------------------


def test_target_roundtrip():
    node = "Analysis.SpecialFunctions.Gamma.beta_integral"
    marker = scoreboard.format_target(node)
    assert scoreboard.parse_target(marker) == node
    # embedded in a larger PR body, with surrounding prose
    body = f"Proves the node.\n\n{marker}\n\n🤖 Prepared with claude via autoform worker"
    assert scoreboard.parse_target(body) == node


def test_target_roundtrip_special_chars():
    node = 'weird "node"/with spaces'
    assert scoreboard.parse_target(scoreboard.format_target(node)) == node


def test_parse_target_rejects_garbage():
    assert scoreboard.parse_target(None) is None
    assert scoreboard.parse_target("") is None
    assert scoreboard.parse_target("no marker here") is None
    # marker present but JSON malformed
    assert scoreboard.parse_target("<!--autoform-target:v1 {not json}-->") is None
    # valid JSON, no node key
    assert scoreboard.parse_target('<!--autoform-target:v1 {"other": 1}-->') is None
    # node present but not a non-empty string
    assert scoreboard.parse_target('<!--autoform-target:v1 {"node": ""}-->') is None
    assert scoreboard.parse_target('<!--autoform-target:v1 {"node": 5}-->') is None
    assert scoreboard.parse_target('<!--autoform-target:v1 {"node": null}-->') is None


# -- scoreboard --------------------------------------------------------------


def test_format_scoreboard_contents_and_meta_roundtrip():
    scores = {"faithfulness": 9, "proof_integrity": 10, "code_quality": None}
    text = scoreboard.format_scoreboard(
        node="grp.cyclic",
        head_sha="a" * 40,
        scores=scores,
        verdict="clean",
        by="worker-1",
        notes={"faithfulness": "matches the statement"},
    )
    assert SCOREBOARD_MARK in text
    assert "✅" in text  # clean verdict icon
    # human-readable table
    assert "| axis | score |" in text
    assert "| faithfulness | 9 |" in text
    assert "| proof_integrity | 10 |" in text
    assert "| code_quality | — |" in text
    assert "**faithfulness:** matches the statement" in text
    # machine meta is present and parseable
    assert META_MARK_RE.search(text)
    meta = scoreboard.parse_meta([{"body": text}])
    assert meta is not None
    assert meta["head_sha"] == "a" * 40
    assert meta["node"] == "grp.cyclic"
    assert meta["scores"] == scores
    assert meta["verdict"] == "clean"
    assert meta["by"] == "worker-1"


def test_parse_meta_newest_wins_and_skips_malformed():
    old = scoreboard.format_scoreboard("n", "b" * 40, {"faithfulness": 4}, "flagged", "w1")
    new = scoreboard.format_scoreboard("n", "c" * 40, {"faithfulness": 9}, "clean", "w2")
    comments = [
        {"body": "plain human comment"},
        {"body": old},
        {"body": new},
        # malformed JSON inside a marker: skipped, must not shadow the newest valid one
        {"body": "<!--autoform-meta:v1 {broken json}-->"},
        # valid JSON but no head_sha string: also skipped
        {"body": '<!--autoform-meta:v1 {"node": "n"}-->'},
        {"body": '<!--autoform-meta:v1 {"head_sha": 7}-->'},
    ]
    meta = scoreboard.parse_meta(comments)
    assert meta is not None
    assert meta["head_sha"] == "c" * 40
    assert meta["verdict"] == "clean"
    assert meta["by"] == "w2"


def test_parse_meta_empty_and_none_bodies():
    assert scoreboard.parse_meta([]) is None
    assert scoreboard.parse_meta([{"body": None}, {"body": ""}, {}]) is None


def test_meta_mark_re_tolerates_whitespace_and_newlines():
    blob = json.dumps(
        {"head_sha": "d" * 40, "node": "n", "scores": {"faithfulness": 8}, "verdict": "clean", "by": "w"},
        indent=2,
    )
    body = "before\n<!--autoform-meta:v1\n" + blob + "\n   -->\nafter"
    meta = scoreboard.parse_meta([{"body": body}])
    assert meta is not None
    assert meta["head_sha"] == "d" * 40
    assert meta["scores"] == {"faithfulness": 8}


# -- in-progress markers -----------------------------------------------------


def _inprogress(head: str, by: str, expires_at: int) -> str:
    data = {"by": by, "expires_at": expires_at, "head": head}
    return f"<!--autoform-review-in-progress {json.dumps(data, sort_keys=True)}-->"


def test_active_inprogress_filters_head_expiry_and_carries_comment_id():
    now = 1_000_000.0
    head = "e" * 40
    comments = [
        {"id": 11, "body": _inprogress(head, "w1", 1_000_500)},        # active, right head
        {"id": 12, "body": _inprogress(head, "w2", 999_000)},          # expired
        {"id": 13, "body": _inprogress(head, "w3", 1_000_000)},        # expires exactly now: not active
        {"id": 14, "body": _inprogress("f" * 40, "w4", 1_000_500)},    # other head
        {"id": 15, "body": "<!--autoform-review-in-progress {bad}-->"},  # malformed JSON
        {"id": 16, "body": "unrelated"},
        {"id": 17, "body": None},
    ]
    active = scoreboard.active_inprogress(comments, head, now=now)
    assert [entry["by"] for entry in active] == ["w1"]
    assert active[0]["_comment_id"] == 11
    assert active[0]["head"] == head
    # no marker survives once time passes every expiry
    assert scoreboard.active_inprogress(comments, head, now=2_000_000.0) == []


def test_format_inprogress_roundtrips_through_active_inprogress():
    head = "9" * 40
    marker = scoreboard.format_inprogress(head, "worker-7", ttl=3600)
    match = INPROGRESS_MARK_RE.search(marker)
    assert match
    data = json.loads(match.group(1))
    assert data["head"] == head
    assert data["by"] == "worker-7"
    # freeze "now" at the recorded expiry minus/plus a margin instead of sleeping
    comments = [{"id": 5, "body": marker}]
    assert scoreboard.active_inprogress(comments, head, now=data["expires_at"] - 10) != []
    assert scoreboard.active_inprogress(comments, head, now=data["expires_at"] + 10) == []
    # wrong head never matches
    assert scoreboard.active_inprogress(comments, "0" * 40, now=data["expires_at"] - 10) == []


# -- counters ----------------------------------------------------------------


def test_counters_get_missing_file_is_zero(tmp_path):
    c = counters.Counters(tmp_path / "counters.json")
    assert c.get("prove-node") == 0


def test_counters_bump_persists_and_returns_new_value(tmp_path):
    path = tmp_path / "state" / "counters.json"  # parent dir is created on save
    c = counters.Counters(path)
    assert c.bump("fix-12-abcdef123456") == 1
    assert c.bump("fix-12-abcdef123456") == 2
    assert c.get("fix-12-abcdef123456") == 2
    # a fresh instance reading the same file sees the persisted value
    assert counters.Counters(path).get("fix-12-abcdef123456") == 2


def test_counters_clear(tmp_path):
    path = tmp_path / "counters.json"
    c = counters.Counters(path)
    c.bump("ci-pr-3")
    c.bump("prove-n")
    c.clear("ci-pr-3")
    assert c.get("ci-pr-3") == 0
    assert c.get("prove-n") == 1  # other keys survive
    c.clear("never-existed")  # no crash on missing key
    assert c.get("never-existed") == 0


def test_counters_refund_bounded_by_max_infra_refunds(tmp_path):
    c = counters.Counters(tmp_path / "counters.json")
    bumps = MAX_INFRA_REFUNDS + 5
    for _ in range(bumps):
        c.bump("prove-n")
    refunds = 0
    while c.refund("prove-n"):
        refunds += 1
        assert refunds <= MAX_INFRA_REFUNDS + 1  # guard against an unbounded loop
    assert refunds == MAX_INFRA_REFUNDS
    assert c.get("prove-n") == bumps - MAX_INFRA_REFUNDS
    assert c.get("infra-prove-n") == MAX_INFRA_REFUNDS
    # budget spent: further refunds are refused and the counter no longer moves
    assert c.refund("prove-n") is False
    assert c.get("prove-n") == bumps - MAX_INFRA_REFUNDS


def test_counters_refund_never_goes_negative(tmp_path):
    c = counters.Counters(tmp_path / "counters.json")
    assert c.refund("prove-zero") is True  # consumes refund budget even at zero
    assert c.get("prove-zero") == 0


def test_counters_corrupt_file_treated_as_empty(tmp_path):
    path = tmp_path / "counters.json"
    path.write_text("{this is not json", encoding="utf-8")
    c = counters.Counters(path)
    assert c.get("prove-n") == 0
    assert c.bump("prove-n") == 1
    assert counters.Counters(path).get("prove-n") == 1  # file was rewritten valid


def test_counters_garbage_values_normalize_to_zero(tmp_path):
    path = tmp_path / "counters.json"
    payload = {"neg": -3, "text": "seven", "frac": 2.5, "nil": None, "arr": [1]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    c = counters.Counters(path)
    for key in payload:
        assert c.get(key) == 0
    # bump on a garbage value restarts the count at 1, not garbage+1
    assert c.bump("neg") == 1
    assert c.bump("text") == 1


def test_counters_non_dict_file_treated_as_empty(tmp_path):
    path = tmp_path / "counters.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    c = counters.Counters(path)
    assert c.get("k") == 0
    assert c.bump("k") == 1
