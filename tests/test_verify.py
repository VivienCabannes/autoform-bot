"""Tests for the shared proof-verification gate (v2: kernel #print axioms) + the
driver integration. The gate's external effects (lakefile probe, file read, `lake
build`, the `#print axioms` probe) are injected, so no Lean toolchain is needed.
The real kernel path is smoke-tested separately against a minimal Lean project.
"""
from __future__ import annotations

import subprocess

from servers.prover.base import ProofResult, ProverAdapter, Run
from servers.prover.driver import prove
from servers.prover.verify import (
    VerifyResult,
    _decls_of,
    _module_of,
    capture_baseline,
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


# --- axiom whitelist -------------------------------------------------------------

def test_standard_axioms_pass_and_are_recorded():
    r = verify_proof("N", "/p", has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "theorem t : 1=1 := rfl", builder=_CLEAN_BUILD,
                     prober=lambda probe, pdir:
                         (0, "'t' depends on axioms: [propext, Classical.choice, Quot.sound]"))
    assert r.ok
    assert r.checks["axioms"] == ["Classical.choice", "Quot.sound", "propext"]


def test_rogue_axiom_fails_naming_it():
    """An axiom-stubbed 'proof' (custom axiom, no sorryAx) must NOT pass the gate."""
    r = verify_proof("N", "/p", has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "theorem t : P := stub", builder=_CLEAN_BUILD,
                     prober=lambda probe, pdir:
                         (0, "'t' depends on axioms: [propext, myEvilStub]"))
    assert not r.ok
    assert "myEvilStub" in r.reason
    assert r.checks["kernel"] == "axiom" and r.checks["rogue_axioms"] == ["myEvilStub"]


def test_ledger_whitelists_project_axioms(tmp_path):
    (tmp_path / "AXIOM_AUDIT.md").write_text(
        "# Audited axioms\n\n"
        "We accept `axiom Physics.continuum_hypothesis` pending Tier-3 work.\n\n"
        "- Analysis.bigAssumption\n",
        encoding="utf-8")
    r = verify_proof("N", str(tmp_path), has_lakefile=True, touched=["A.lean"],
                     reader=lambda p: "theorem t : P := ledgered", builder=_CLEAN_BUILD,
                     prober=lambda probe, pdir:
                         (0, "'t' depends on axioms: [Physics.continuum_hypothesis, "
                             "Analysis.bigAssumption, propext]"))
    assert r.ok


def test_ledger_parser_is_forgiving_but_not_greedy():
    from servers.prover.verify import parse_axiom_ledger

    text = (
        "# Ledger\n"
        "Prose about the project. Overview words must not become allowances.\n"
        "```lean\naxiom Foo.bar : Nat\n```\n"
        "inline mention of axiom Baz.qux here\n"
        "- `List.item'`\n"
        "* Another.one\n"
        "- not a single name\n"
    )
    names = parse_axiom_ledger(text)
    assert names == {"Foo.bar", "Baz.qux", "List.item'", "Another.one"}


# --- baseline change-attribution (real git repo in tmp_path) --------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "Existing.lean").write_text("theorem old : True := trivial\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_baseline_pre_existing_dirty_file_is_not_attributed(tmp_path):
    """A pre-existing dirty file must NOT let a run that landed nothing pass."""
    repo = _make_repo(tmp_path)
    (repo / "Existing.lean").write_text("theorem old : True := trivial -- user WIP\n", encoding="utf-8")

    baseline = capture_baseline(str(repo))
    assert baseline.captured and "Existing.lean" in baseline.dirty_hashes

    # ... the run happens, lands NOTHING ...
    r = verify_proof("N", str(repo), baseline=baseline, has_lakefile=True,
                     builder=_CLEAN_BUILD, prober=_CLEAN_PROBE)
    assert not r.ok and "nothing landed" in r.reason


def test_baseline_new_file_is_attributed_and_verified(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "Existing.lean").write_text("-- pre-existing user edit\n", encoding="utf-8")
    baseline = capture_baseline(str(repo))

    # The run lands a NEW file.
    (repo / "Fresh.lean").write_text("theorem fresh : True := trivial\n", encoding="utf-8")
    r = verify_proof("N", str(repo), baseline=baseline, has_lakefile=True,
                     builder=_CLEAN_BUILD,
                     prober=lambda probe, pdir: (0, "'fresh' depends on axioms: [propext]"))
    assert r.ok
    # Only the run's file is verified — the pre-existing dirty file is excluded.
    assert r.checks["files"] == ["Fresh.lean"]


def test_baseline_rewritten_dirty_file_is_attributed(tmp_path):
    """A file dirty at baseline whose CONTENT the run changed counts as touched."""
    repo = _make_repo(tmp_path)
    (repo / "Existing.lean").write_text("-- user WIP\n", encoding="utf-8")
    baseline = capture_baseline(str(repo))

    (repo / "Existing.lean").write_text("theorem proved : True := trivial\n", encoding="utf-8")
    r = verify_proof("N", str(repo), baseline=baseline, has_lakefile=True,
                     builder=_CLEAN_BUILD, prober=_CLEAN_PROBE)
    assert r.ok and r.checks["files"] == ["Existing.lean"]


def test_baseline_committed_proof_is_attributed(tmp_path):
    """A worker that COMMITTED its proof is covered via the baseline head diff."""
    repo = _make_repo(tmp_path)
    baseline = capture_baseline(str(repo))

    (repo / "Fresh.lean").write_text("theorem fresh : True := trivial\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "worker proof")
    r = verify_proof("N", str(repo), baseline=baseline, has_lakefile=True,
                     builder=_CLEAN_BUILD, prober=_CLEAN_PROBE)
    assert r.ok and r.checks["files"] == ["Fresh.lean"]


def test_baseline_capture_never_raises_outside_git(tmp_path):
    b = capture_baseline(str(tmp_path / "not-a-repo"))
    assert b.captured is False  # gate falls back to no-baseline behaviour


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
                verifier=lambda node, pdir, baseline=None: VerifyResult(False, "sorryAx", {"verified": True}))
    assert res.status == "failed" and "verification gate" in res.reason and res.meta["claimed_proved"] is True


def test_driver_passes_real_proved():
    res = prove(_FakeAdapter("proved"), "N", "spec", "/p", max_steers=0,
                verifier=lambda node, pdir, baseline=None: VerifyResult(True, "", {"verified": True, "kernel": "clean"}))
    assert res.status == "proved" and res.meta["verify"]["kernel"] == "clean"


def test_driver_gate_only_runs_on_proved():
    n = {"c": 0}
    prove(_FakeAdapter("failed"), "N", "spec", "/p", max_steers=0,
          verifier=lambda node, pdir, baseline=None: (n.__setitem__("c", n["c"] + 1), VerifyResult(True))[1])
    assert n["c"] == 0


def test_driver_gate_disabled_when_none():
    res = prove(_FakeAdapter("proved"), "N", "spec", "/p", max_steers=0, verifier=None)
    assert res.status == "proved"
