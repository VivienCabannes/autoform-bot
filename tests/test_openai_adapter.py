"""OpenAI-compatible / Avocado backend — fake transports, no network.

The adapter is a request/response prover (SteeringCapability.NONE): one
chat-completions call, land the fenced Lean file, let the honesty gate verify.
Everything here drives it through injected transports; the Avocado preset's
env plumbing is exercised with monkeypatched variables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.prover.base import EventKind, SteeringCapability
from servers.prover.driver import prove
from servers.prover.openai_adapter import OpenAICompatAdapter
from servers.prover.verify import VerifyResult

from tests.test_steering_phase_a import _FakeSteerer, _FakeVerifier

PROOF_REPLY = """Here is the completed file.

```lean
import Mathlib

theorem chernoff : True := trivial
```
"""


def _graph(tmp_path: Path, lean_file: str | None = "Prob/Chernoff.lean") -> str:
    node: dict = {"id": "Chernoff bound", "tier": 2, "kind": "theorem"}
    if lean_file:
        node["lean_file"] = lean_file
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps({"nodes": {"Chernoff bound": node}}), encoding="utf-8")
    return str(gp)


class FakeTransport:
    """Scripted response; records the request for assertions."""

    def __init__(self, response: dict | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload,
                           "timeout": timeout})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _reply(text: str, *, prompt_tokens: int = 100, completion_tokens: int = 50) -> dict:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AUTOFORM_OPENAI_MODEL", "test-model")


def test_capability_is_none():
    assert OpenAICompatAdapter.steering is SteeringCapability.NONE


def test_proved_flow_lands_file_and_reports_usage(tmp_path, keyed):
    transport = FakeTransport(_reply(PROOF_REPLY))
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path), transport=transport)

    result = prove(adapter, "Chernoff bound", "prove the bound", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=None)

    assert result.proved
    assert result.landed_files == 1
    landed = tmp_path / "Prob" / "Chernoff.lean"
    assert landed.exists()
    assert "theorem chernoff : True := trivial" in landed.read_text()
    # Usage captured (OpenAI names normalized) and nested by the driver.
    assert result.meta["usage"]["worker"] == {"input_tokens": 100, "output_tokens": 50,
                                              "turns": 1}
    # The request carried the discipline + spec and the auth header.
    req = transport.calls[0]
    assert req["url"].endswith("/chat/completions")
    assert req["headers"]["Authorization"] == "Bearer sk-test"
    body = json.dumps(req["payload"])
    assert "FAILED" in body and "Chernoff bound" in body


def test_honest_failed_lands_nothing(tmp_path, keyed):
    transport = FakeTransport(_reply("FAILED — the bound needs a missing lemma"))
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path), transport=transport)
    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=None)
    assert result.status == "failed"
    assert "missing lemma" in result.reason
    assert not (tmp_path / "Prob" / "Chernoff.lean").exists()


def test_no_fence_no_failed_is_failed(tmp_path, keyed):
    transport = FakeTransport(_reply("I think the theorem is true because reasons."))
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path), transport=transport)
    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=None)
    assert result.status == "failed"
    assert "no Lean code block" in result.reason


def test_file_header_fallback_and_sanitization(tmp_path, keyed):
    # No lean_file in the graph → the model's -- FILE: header decides…
    reply = "```lean\n-- FILE: Prob/FromHeader.lean\nimport Mathlib\ntheorem t : True := trivial\n```"
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path, lean_file=None),
                                  transport=FakeTransport(_reply(reply)))
    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=None)
    assert result.proved
    assert (tmp_path / "Prob" / "FromHeader.lean").exists()

    # …but an escaping path is rejected and the run fails honestly.
    evil = "```lean\n-- FILE: ../outside.lean\ntheorem t : True := trivial\n```"
    adapter2 = OpenAICompatAdapter(graph_path=_graph(tmp_path, lean_file=None),
                                   transport=FakeTransport(_reply(evil)))
    result2 = prove(adapter2, "Chernoff bound", "spec", str(tmp_path),
                    steerer=_FakeSteerer(), verifier=None)
    assert result2.status == "failed"
    assert "cannot resolve a target file" in result2.reason
    assert not (tmp_path.parent / "outside.lean").exists()


def test_transport_error_fails_cleanly(tmp_path, keyed):
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path),
                                  transport=FakeTransport(OSError("connection refused")))
    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=None)
    assert result.status == "failed"
    assert "transport error" in result.reason
    assert result.meta["usage"]["worker"]["turns"] == 1


def test_missing_credential_is_a_clean_start_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AUTOFORM_OPENAI_MODEL", "test-model")
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path),
                                  transport=FakeTransport(_reply("x")))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        adapter.start("N", "spec", str(tmp_path))


def test_avocado_preset_defaults_and_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "meta-key")
    transport = FakeTransport(_reply(PROOF_REPLY))
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path), preset="avocado",
                                  transport=transport)
    assert adapter.name == "avocado"
    prove(adapter, "Chernoff bound", "spec", str(tmp_path),
          steerer=_FakeSteerer(), verifier=None)
    req = transport.calls[0]
    assert req["url"] == "https://api.meta.ai/v1/chat/completions"     # public preset
    assert req["payload"]["model"] == "muse-spark-1.1"
    assert req["headers"]["Authorization"] == "Bearer meta-key"

    # The internal gateway overrides everything without code changes:
    monkeypatch.setenv("AUTOFORM_AVOCADO_BASE_URL", "https://gw.internal.meta.com/llm/v1")
    monkeypatch.setenv("AUTOFORM_AVOCADO_MODEL", "muse1.1")
    monkeypatch.setenv("AUTOFORM_AVOCADO_KEY_VAR", "INTERNAL_LLM_TOKEN")
    monkeypatch.setenv("INTERNAL_LLM_TOKEN", "internal-secret")
    monkeypatch.setenv("AUTOFORM_AVOCADO_EXTRA_HEADERS", '{"X-Meta-Route": "avocado"}')
    transport2 = FakeTransport(_reply(PROOF_REPLY))
    adapter2 = OpenAICompatAdapter(graph_path=_graph(tmp_path), preset="avocado",
                                   transport=transport2)
    prove(adapter2, "Chernoff bound", "spec", str(tmp_path),
          steerer=_FakeSteerer(), verifier=None)
    req2 = transport2.calls[0]
    assert req2["url"] == "https://gw.internal.meta.com/llm/v1/chat/completions"
    assert req2["payload"]["model"] == "muse1.1"
    assert req2["headers"]["Authorization"] == "Bearer internal-secret"
    assert req2["headers"]["X-Meta-Route"] == "avocado"


def test_gate_rejection_downgrades_without_fold(tmp_path, keyed):
    """Capability NONE: a rejected claim downgrades immediately — no corrective
    turn is attempted against a request/response backend."""
    transport = FakeTransport(_reply(PROOF_REPLY))
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path), transport=transport)
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="sorry remains")])

    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"
    assert result.meta["claimed_proved"] is True
    assert verifier.calls == 1
    assert len(transport.calls) == 1        # exactly one API call, no fold retry


def test_clobber_restored_when_gate_rejects(tmp_path, keyed):
    """Request/response backends write BEFORE the gate runs; if the gate rejects,
    previously-good (uncommitted) content at the target must be restored, not lost."""
    landed = tmp_path / "Prob" / "Chernoff.lean"
    landed.parent.mkdir(parents=True)
    prior = "import Mathlib\n\ntheorem chernoff : True := trivial  -- GOOD prior proof\n"
    landed.write_text(prior, encoding="utf-8")

    transport = FakeTransport(_reply(PROOF_REPLY))
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path), transport=transport)
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="sorry remains")])

    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"
    assert result.meta.get("landed_restored") is True
    assert "landed_backup" not in result.meta          # content consumed, not persisted
    assert landed.read_text() == prior                  # prior good content restored verbatim


def test_clobber_new_file_removed_when_gate_rejects(tmp_path, keyed):
    """When the run CREATED the target, a gate rejection removes it — there was no
    prior content, so leaving a rejected candidate on disk would be the clobber."""
    landed = tmp_path / "Prob" / "Chernoff.lean"
    assert not landed.exists()
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path),
                                  transport=FakeTransport(_reply(PROOF_REPLY)))
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="does not compile")])

    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"
    assert result.meta.get("landed_restored") is True
    assert not landed.exists()                          # newly-created rejected file removed


def test_gate_pass_keeps_landed_file_and_drops_backup(tmp_path, keyed):
    """When the gate accepts, the landed proof stays and the backup bookkeeping is
    dropped (it must never reach the ledger)."""
    landed = tmp_path / "Prob" / "Chernoff.lean"
    landed.parent.mkdir(parents=True)
    landed.write_text("-- old placeholder\n", encoding="utf-8")
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path),
                                  transport=FakeTransport(_reply(PROOF_REPLY)))
    verifier = _FakeVerifier([VerifyResult(ok=True)])

    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.proved
    assert "theorem chernoff : True := trivial" in landed.read_text()  # new proof kept
    assert "landed_backup" not in result.meta
    assert "landed_restored" not in result.meta         # no restore happened on the pass path


def test_clobber_restore_is_byte_exact_even_for_non_utf8_prior(tmp_path, keyed):
    """The prior is backed up as RAW BYTES: a non-UTF-8 / CRLF target must neither
    crash the run (no decode) nor be mangled on restore (no newline translation)."""
    landed = tmp_path / "Prob" / "Chernoff.lean"
    landed.parent.mkdir(parents=True)
    prior = b"import Mathlib\r\ntheorem chernoff : True := trivial\r\n-- raw \x89\xff bytes\r\n"
    landed.write_bytes(prior)

    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path),
                                  transport=FakeTransport(_reply(PROOF_REPLY)))
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="rejected")])

    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"                    # run did not crash on the non-UTF-8 prior
    assert result.meta.get("landed_restored") is True
    assert landed.read_bytes() == prior                 # CRLF + non-UTF-8 bytes restored verbatim


def test_backup_tracks_the_file_that_actually_lands(tmp_path, keyed):
    """samples>1 + header fallback: if an earlier candidate resolves a target it
    CANNOT write and a later candidate lands a DIFFERENT file, backup/restore must
    track the file that actually landed — not the first target resolved."""
    prob = tmp_path / "Prob"
    prob.mkdir(parents=True)
    (prob / "A.lean").mkdir()                            # A is a dir → writing it as a file fails
    b = prob / "B.lean"
    b_prior = "import Mathlib\ntheorem b_good : True := trivial\n"
    b.write_text(b_prior, encoding="utf-8")

    resp = {"choices": [
        {"message": {"content": "```lean\n-- FILE: Prob/A.lean\nimport Mathlib\ntheorem a : True := trivial\n```"},
         "finish_reason": "stop"},
        {"message": {"content": "```lean\n-- FILE: Prob/B.lean\nimport Mathlib\ntheorem b_bad : True := trivial\n```"},
         "finish_reason": "stop"},
    ], "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path, lean_file=None), samples=2,
                                  transport=FakeTransport(resp))
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="rejected")])

    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"
    assert result.meta.get("landed_restored") is True
    assert b.read_text() == b_prior                      # the LANDED file is the one restored


def test_restore_leaves_unreadable_prior_untouched(tmp_path):
    """existed=True but prior=None (unreadable at land time): the driver must NOT
    delete the file (that would also lose data) and reports landed_restored=False."""
    from servers.prover.base import ProofResult
    from servers.prover.driver import _restore_landed

    f = tmp_path / "T.lean"
    f.write_text("rejected candidate on disk\n", encoding="utf-8")
    result = ProofResult(status="failed",
                         meta={"landed_backup": {"path": str(f), "existed": True, "prior": None}})

    _restore_landed(result)

    assert result.meta["landed_restored"] is False
    assert f.read_text() == "rejected candidate on disk\n"  # left as-is, NOT deleted
    assert "landed_backup" not in result.meta              # content consumed


def test_make_adapter_knows_openai_and_avocado(monkeypatch):
    from servers.prover.server import _make_adapter

    monkeypatch.setenv("AUTOFORM_OPENAI_MODEL", "m")
    a = _make_adapter("openai", "g.json", 60)
    assert a.name == "openai" and a.steering is SteeringCapability.NONE
    b = _make_adapter("avocado", "g.json", 60)
    assert b.name == "avocado"
    assert b._model  # the public muse-spark default (env may override)


def test_event_stream_is_normalized(tmp_path, keyed):
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path),
                                  transport=FakeTransport(_reply(PROOF_REPLY)))
    run = adapter.start("Chernoff bound", "spec", str(tmp_path))
    kinds = [e.kind for e in adapter.events(run)]
    assert kinds[0] is EventKind.TOOL          # the API call itself
    assert EventKind.MESSAGE in kinds          # the model's reply
    assert EventKind.EDIT in kinds             # the landed file
    assert kinds[-1] is EventKind.RESULT
    # Re-entry yields nothing (request/response is one-shot).
    assert list(adapter.events(run)) == []


def test_multi_fence_reply_lands_the_largest_fence(tmp_path, keyed):
    """A chatty reply (sketch fence + full-file fence) must land the FULL file —
    landing the first/snippet fence produced an end-to-end false proved."""
    reply = (
        "First a sketch:\n```lean\nexample : True := trivial\n```\n"
        "Now the complete file:\n```lean\nimport Mathlib\n\n"
        "theorem chernoff : True := trivial\n\n"
        "theorem helper : 1 = 1 := rfl\n```\n")
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path),
                                  transport=FakeTransport(_reply(reply)))
    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=None)
    assert result.proved
    landed = (tmp_path / "Prob" / "Chernoff.lean").read_text()
    assert "theorem chernoff" in landed and "import Mathlib" in landed
    assert landed.strip() != "example : True := trivial"


def test_second_sample_proves_after_first_fails(tmp_path, keyed):
    """Requesting n candidates must examine all of them — aborting on the first
    FAILED choice defeated the point of sampling."""
    resp = {"choices": [
        {"message": {"content": "FAILED — first attempt gave up"}, "finish_reason": "stop"},
        {"message": {"content": PROOF_REPLY}, "finish_reason": "stop"},
    ], "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path), samples=2,
                                  transport=FakeTransport(resp))
    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=None)
    assert result.proved
    assert (tmp_path / "Prob" / "Chernoff.lean").exists()


def test_graph_lean_file_is_sanitized(tmp_path, keyed):
    """A traversal path in the plan's own lean_file pin must not escape."""
    adapter = OpenAICompatAdapter(graph_path=_graph(tmp_path, lean_file="../ESCAPE.lean"),
                                  transport=FakeTransport(_reply(PROOF_REPLY)))
    result = prove(adapter, "Chernoff bound", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=None)
    assert result.status == "failed"
    assert not (tmp_path.parent / "ESCAPE.lean").exists()
