"""Tests for the shared proof-verification gate + its driver integration.

No Lean toolchain needed: the gate's external effects (lakefile probe, file read,
``lake`` build) are injected, and the driver integration uses a fake adapter +
fake verifier.
"""
from __future__ import annotations

from servers.prover.base import ProofResult, ProverAdapter, Run
from servers.prover.driver import prove
from servers.prover.verify import VerifyResult, has_sorry, verify_proof


# --- the sorry scanner ---------------------------------------------------------

def test_has_sorry_detects_real_markers():
    assert has_sorry("theorem foo : True := by sorry")
    assert has_sorry("by admit")
    assert has_sorry("exact sorryAx _")


def test_has_sorry_ignores_comments_and_identifiers():
    assert not has_sorry("-- this proof is sorry-free\ntheorem foo : True := trivial")
    assert not has_sorry("/- a sorry hidden in a block comment -/\ndef x := 1")
    assert not has_sorry("def sorryHandler := 1")          # whole-word
    assert not has_sorry("theorem ok : 1 = 1 := rfl")


# --- the gate ------------------------------------------------------------------

def test_verify_skips_without_lakefile():
    r = verify_proof("Foo", "/tmp/nope", has_lakefile=False)
    assert r.ok and r.checks.get("verified") is False and r.checks.get("skipped") == "no lakefile"


def test_verify_fails_when_nothing_landed():
    r = verify_proof("Foo", "/proj", has_lakefile=True, touched=[])
    assert not r.ok and "nothing to verify" in r.reason


def test_verify_fails_on_sorry():
    r = verify_proof("Foo", "/proj", has_lakefile=True, touched=["A/Foo.lean"],
                     reader=lambda p: "theorem foo := by sorry",
                     builder=lambda f, d: (0, ""))
    assert not r.ok and "sorry" in r.reason and r.checks["sorry_in"] == "A/Foo.lean"


def test_verify_fails_on_build_error():
    r = verify_proof("Foo", "/proj", has_lakefile=True, touched=["A/Foo.lean"],
                     reader=lambda p: "theorem foo : 1 = 1 := rfl",
                     builder=lambda f, d: (1, "error: unknown identifier 'foo'"))
    assert not r.ok and "does not compile" in r.reason and r.checks["build_fail"] == "A/Foo.lean"


def test_verify_passes_clean():
    r = verify_proof("Foo", "/proj", has_lakefile=True, touched=["A/Foo.lean"],
                     reader=lambda p: "theorem foo : 1 = 1 := rfl",
                     builder=lambda f, d: (0, ""))
    assert r.ok and r.checks["build"] == "clean" and r.checks["sorry"] == "none"


def test_verify_node_file_fallback(tmp_path):
    # No git changes (not a repo) → fall back to the node's module path A.B → A/B.lean
    (tmp_path / "lakefile.toml").write_text("")
    f = tmp_path / "Foo" / "Bar.lean"
    f.parent.mkdir()
    f.write_text("theorem t : 1 = 1 := rfl")
    r = verify_proof("Foo.Bar", str(tmp_path), builder=lambda fp, d: (0, ""))
    assert r.ok and r.checks["files"] == ["Foo/Bar.lean"]


# --- driver integration: the gate overrides a false "proved" claim -------------

class _FakeAdapter(ProverAdapter):
    name = "fake"

    def __init__(self, status: str) -> None:
        self._status = status

    def start(self, node, spec, project_dir):
        return Run(backend=self.name, goal=spec, project_dir=project_dir)

    def events(self, run):
        return iter(())

    def steer(self, run, message):
        pass

    def result(self, run):
        return ProofResult(status=self._status, proof_text="claimed!", backend=self.name)


def test_driver_gate_downgrades_false_proved():
    res = prove(_FakeAdapter("proved"), "Foo", "spec", "/proj", max_steers=0,
                verifier=lambda node, pdir: VerifyResult(False, "still contains a sorry", {"verified": True}))
    assert res.status == "failed"
    assert "verification gate" in res.reason
    assert res.meta.get("claimed_proved") is True


def test_driver_gate_passes_real_proved():
    res = prove(_FakeAdapter("proved"), "Foo", "spec", "/proj", max_steers=0,
                verifier=lambda node, pdir: VerifyResult(True, "", {"verified": True, "build": "clean"}))
    assert res.status == "proved"
    assert res.meta["verify"]["build"] == "clean"


def test_driver_gate_not_run_on_failed_claim():
    called = {"n": 0}

    def v(node, pdir):
        called["n"] += 1
        return VerifyResult(True)

    res = prove(_FakeAdapter("failed"), "Foo", "spec", "/proj", max_steers=0, verifier=v)
    assert res.status == "failed" and called["n"] == 0   # gate only runs on a claimed proved


def test_driver_gate_disabled_when_none():
    res = prove(_FakeAdapter("proved"), "Foo", "spec", "/proj", max_steers=0, verifier=None)
    assert res.status == "proved"   # no gate, claim stands (used by tests/edge configs)
