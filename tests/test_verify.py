"""Tests for the shared proof-verification gate (v2: kernel #print axioms) + the
driver integration. The gate's external effects (lakefile probe, file read, `lake
build`, the `#print axioms` probe) are injected, so no Lean toolchain is needed.
The real kernel path is smoke-tested separately against a minimal Lean project.
"""
from __future__ import annotations

from servers.prover.base import ProofResult, ProverAdapter, Run
from servers.prover.driver import prove
from servers.prover.verify import (
    VerifyResult,
    _decls_of,
    _module_of,
    has_sorry,
    parse_porcelain_z,
    verify_proof,
)

# convenient fakes
_CLEAN_BUILD = lambda pdir: (0, "")                                   # noqa: E731
_CLEAN_PROBE = lambda probe, pdir: (0, "'foo' depends on axioms: [propext]")  # noqa: E731


# --- pure helpers --------------------------------------------------------------

def test_has_sorry():
    assert has_sorry("by sorry") and has_sorry("by admit") and has_sorry("exact sorryAx _")
    assert not has_sorry("-- sorry-free\ntheorem t : 1=1 := rfl")
    assert not has_sorry("/- a sorry here -/\ndef x := 1")
    assert not has_sorry("def sorryHandler := 1")


def test_parse_porcelain_z_skips_deletions_and_renames():
    # " M a.lean\0?? b.lean\0 D gone.lean\0R  new.lean\0old.lean\0 M c.txt\0"
    data = b" M a.lean\x00?? b.lean\x00 D gone.lean\x00R  new.lean\x00old.lean\x00 M c.txt\x00"
    assert parse_porcelain_z(data) == ["a.lean", "b.lean", "new.lean"]


def test_module_of():
    assert _module_of("A/B/C.lean", "/proj") == "A.B.C"
    assert _module_of("/proj/Sun/Core.lean", "/proj") == "Sun.Core"
    assert _module_of("notlean.txt", "/proj") is None


def test_decls_of_qualifies_by_namespace():
    src = "namespace Sun\ntheorem foo : True := trivial\ndef bar := 1\nend Sun\nlemma top : True := trivial"
    assert _decls_of(src) == ["Sun.foo", "Sun.bar", "top"]


# --- the gate (injected seams) -------------------------------------------------

def test_fail_closed_without_lakefile():
    r = verify_proof("N", "/p", has_lakefile=False)            # require_lakefile defaults True
    assert not r.ok and "no lakefile" in r.reason and r.checks["verified"] is False


def test_skip_allowed_when_not_required():
    r = verify_proof("N", "/p", has_lakefile=False, require_lakefile=False)
    assert r.ok and r.checks.get("skipped") == "no lakefile"


def test_fail_when_nothing_landed():
    r = verify_proof("N", "/p", has_lakefile=True, touched=[])
    assert not r.ok and "nothing to verify" in r.reason


def test_literal_sorry_prefilter():
    r = verify_proof("N", "/p", has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "theorem t := by sorry", builder=_CLEAN_BUILD, prober=_CLEAN_PROBE)
    assert not r.ok and r.checks["sorry_in"] == "A.lean"


def test_build_error_fails():
    r = verify_proof("N", "/p", has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "theorem t : 1=1 := rfl",
                     builder=lambda pdir: (1, "error: unknown identifier"), prober=_CLEAN_PROBE)
    assert not r.ok and r.checks["build"] == "error"


def test_sorryAx_fails_even_with_clean_source_and_build():
    # The authoritative check: source has no literal sorry, build is clean, but the
    # kernel shows sorryAx (a transitive/imported gap) → must fail.
    r = verify_proof("N", "/p", has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "theorem t : True := helper",   # no literal sorry
                     builder=_CLEAN_BUILD,
                     prober=lambda probe, pdir: (0, "'t' depends on axioms: [sorryAx]"))
    assert not r.ok and r.checks["kernel"] == "sorryAx"


def test_clean_proof_passes():
    r = verify_proof("N", "/p", has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "theorem t : 1=1 := rfl", builder=_CLEAN_BUILD,
                     prober=lambda probe, pdir: (0, "'t' does not depend on any axioms"))
    assert r.ok and r.checks["kernel"] == "clean"


def test_unverifiable_when_probe_has_no_report():
    # probe produced no axiom report (e.g. module not built / import failed) → fail-safe.
    r = verify_proof("N", "/p", has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "theorem t : 1=1 := rfl", builder=_CLEAN_BUILD,
                     prober=lambda probe, pdir: (1, "error: unknown package"))
    assert not r.ok and r.checks["kernel"] == "unverified"


def test_no_decls_passes_after_build():
    r = verify_proof("N", "/p", has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "import Foo.Bar\nnamespace X\nend X", builder=_CLEAN_BUILD,
                     prober=_CLEAN_PROBE)
    assert r.ok and r.checks["kernel"] == "no-decls"


# --- driver integration --------------------------------------------------------

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


def test_driver_downgrades_false_proved():
    res = prove(_FakeAdapter("proved"), "N", "spec", "/p", max_steers=0,
                verifier=lambda node, pdir: VerifyResult(False, "sorryAx", {"verified": True}))
    assert res.status == "failed" and "verification gate" in res.reason and res.meta["claimed_proved"] is True


def test_driver_passes_real_proved():
    res = prove(_FakeAdapter("proved"), "N", "spec", "/p", max_steers=0,
                verifier=lambda node, pdir: VerifyResult(True, "", {"verified": True, "kernel": "clean"}))
    assert res.status == "proved" and res.meta["verify"]["kernel"] == "clean"


def test_driver_gate_only_runs_on_proved():
    n = {"c": 0}
    prove(_FakeAdapter("failed"), "N", "spec", "/p", max_steers=0,
          verifier=lambda node, pdir: (n.__setitem__("c", n["c"] + 1), VerifyResult(True))[1])
    assert n["c"] == 0


def test_driver_gate_disabled_when_none():
    res = prove(_FakeAdapter("proved"), "N", "spec", "/p", max_steers=0, verifier=None)
    assert res.status == "proved"
