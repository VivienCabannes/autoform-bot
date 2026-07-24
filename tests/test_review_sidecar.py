"""Regression tests for the review_status.json sidecar persistence hardening.

The sidecar holds the irreplaceable human verdicts, so two properties matter:
  * writes are atomic (an interrupted write never leaves a half-file), and
  * a corrupt sidecar is preserved + warned, never silently discarded.
"""
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm  # noqa: E402


def test_atomic_save_leaves_no_temp_file():
    d = Path(tempfile.mkdtemp())
    p = d / "review_status.json"
    rm.save_sidecar(p, rm.empty_sidecar())
    assert p.is_file()
    assert not (d / "review_status.json.tmp").exists()


def test_roundtrip_preserves_human_verdict():
    d = Path(tempfile.mkdtemp())
    p = d / "review_status.json"
    sc = rm.empty_sidecar()
    sc["reviews"]["n1"] = {"human": {"verdict": "clean", "score": 5, "by": "jack"}}
    rm.save_sidecar(p, sc)
    assert rm.load_sidecar(p)["reviews"]["n1"]["human"]["verdict"] == "clean"


def test_corrupt_sidecar_is_backed_up_not_lost():
    d = Path(tempfile.mkdtemp())
    p = d / "review_status.json"
    p.write_text("{ this is : not json,,,")
    out = rm.load_sidecar(p)
    assert out["reviews"] == {}                      # falls back to fresh
    assert (d / "review_status.json.corrupt").exists()  # but original preserved
    assert not p.exists()                            # corrupt file renamed aside


def test_second_corruption_does_not_clobber_first_backup():
    d = Path(tempfile.mkdtemp())
    p = d / "review_status.json"
    p.write_text("bad1{")
    rm.load_sidecar(p)
    p.write_text("bad2{")
    rm.load_sidecar(p)
    assert (d / "review_status.json.corrupt").exists()
    assert (d / "review_status.json.corrupt.1").exists()


def test_missing_sidecar_is_fresh_not_error():
    d = Path(tempfile.mkdtemp())
    assert rm.load_sidecar(d / "nope.json") == rm.empty_sidecar()


def test_non_object_root_is_treated_as_corrupt():
    d = Path(tempfile.mkdtemp())
    p = d / "review_status.json"
    p.write_text("[1, 2, 3]")  # valid JSON, wrong shape
    out = rm.load_sidecar(p)
    assert out["reviews"] == {}
    assert (d / "review_status.json.corrupt").exists()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
