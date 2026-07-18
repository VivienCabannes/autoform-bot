"""Generic OpenAI-compatible HTTP backend — and the Meta ``avocado`` preset.

A fourth swappable backend: any endpoint speaking the OpenAI Chat Completions
wire format (the industry-standard shape internal gateways, vLLM/SGLang, and
Meta's public Model API all expose). Unlike the CLI backends this is a pure
**request/response prover** — no session, no tools, no mid-run channel — so it
is the first adapter at ``SteeringCapability.NONE``: the driver never live-
judges it and never folds into it; a rejected claim downgrades, and correction
happens at the next whole attempt (dispatch-level retry).

**Sample mode (what this implements).** One chat-completions call asks the
model for the COMPLETE contents of the node's target ``.lean`` file (spec +
no-cheating discipline in the prompt); the adapter extracts the fenced Lean
block, lands it at the target path, and claims ``proved`` — which the driver's
kernel honesty gate then independently verifies, so an unknown model is safe
on day one. An honest ``FAILED — <reason>`` reply lands nothing. (The agentic
tool-loop mode of proposal #4 is future work; see docs/avocado-handoff.md.)

**The ``avocado`` preset.** "Avocado" is Meta's internal codename for the
model released publicly as **Muse Spark** (per CNBC, 2026-07-09); the public
surface is the Meta Model API — base ``https://api.meta.ai/v1``, model id
``muse-spark-1.1``, Bearer auth, drop-in OpenAI-compatible. The preset
defaults to that PUBLIC surface; the Meta-internal deployment (endpoint,
auth, and whether the internal model id is ``muse1.1``) is unknown from
outside and is exactly what the env overrides below exist for. The full
work-laptop checklist lives in ``docs/avocado-handoff.md``.

**Interface assumptions** (the codex-adapter discipline): this targets OpenAI
Chat Completions — ``POST {base}/chat/completions`` with ``{model, messages,
n, max_tokens}``, choices at ``choices[i].message.content``, token usage at
``usage.{prompt_tokens, completion_tokens}``. Non-streaming. The proved/failed
verdict rests on the reply's ``FAILED — <reason>`` line and on whether a Lean
block actually landed — never on schema details — so a gateway with a slightly
different envelope degrades to an honest failure, not a false proof. Every
guess is overridable:

    AUTOFORM_OPENAI_BASE_URL / AUTOFORM_AVOCADO_BASE_URL
    AUTOFORM_OPENAI_MODEL    / AUTOFORM_AVOCADO_MODEL
    AUTOFORM_OPENAI_KEY_VAR  / AUTOFORM_AVOCADO_KEY_VAR   (NAME of the env var
        holding the credential, so an internal gateway's own var name is reused)
    AUTOFORM_OPENAI_EXTRA_HEADERS / AUTOFORM_AVOCADO_EXTRA_HEADERS  (JSON dict —
        internal gateways often want an extra identity/routing header)
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._cli_common import _build_spec_prompt, _failure_reason, _looks_failed
from .base import Event, EventKind, ProofResult, ProverAdapter, Run, SteeringCapability

logger = logging.getLogger(__name__)

#: Per-preset defaults: (base_url, model, credential env-var NAME).
_PRESETS: dict[str, tuple[str, str, str]] = {
    # Public OpenAI endpoint; model deliberately unset (choose explicitly).
    "openai": ("https://api.openai.com/v1", "", "OPENAI_API_KEY"),
    # Meta Model API public preview (Avocado = Muse Spark's codename). The
    # INTERNAL deployment's endpoint/model/auth are unverified from outside —
    # override via the AUTOFORM_AVOCADO_* env vars (docs/avocado-handoff.md).
    "avocado": ("https://api.meta.ai/v1", "muse-spark-1.1", "MODEL_API_KEY"),
}

SAMPLE_SYSTEM_PROMPT = """\
You are a Lean 4 / Mathlib prover producing the COMPLETE contents of one file.

Hard rules (violations make the work worthless):
- Do NOT use `sorry`, `admit`, `native_decide`, or introduce any `axiom`.
- Do NOT weaken, restate, or alter the target statement to make it provable.
- Output the ENTIRE target file (imports included) inside ONE fenced code block
  starting with ```lean and ending with ```. Nothing outside the fence matters.
- If you cannot produce a complete honest proof, reply with a single line
  `FAILED — <the concrete blocker>` and NO code fence.

Your output is independently verified by the Lean kernel (build + axiom audit);
a dishonest file is always caught and rejected, so an honest FAILED is strictly
better than a fake proof."""

_LEAN_FENCE_RE = re.compile(r"```lean\s*\n(.*?)```", re.DOTALL)
_FILE_HEADER_RE = re.compile(r"^\s*--\s*FILE:\s*(\S+)\s*\n", re.IGNORECASE)


def _urllib_transport(url: str, headers: dict[str, str], payload: dict[str, Any],
                      timeout: float) -> dict[str, Any]:
    """Default transport: one blocking POST via stdlib urllib (no new deps)."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https URL from config
        return json.loads(resp.read().decode("utf-8"))


def _env(preset: str, suffix: str) -> str:
    """Preset-specific env override with the generic OPENAI_* as fallback."""
    return (os.environ.get(f"AUTOFORM_{preset.upper()}_{suffix}", "")
            or os.environ.get(f"AUTOFORM_OPENAI_{suffix}", "")).strip()


def _resolve_target_file(graph_path: str, node: str, project_dir: str) -> Path | None:
    """The node's Lean file: the plan's explicit ``lean_file`` pin, when present."""
    try:
        graph = json.loads(Path(graph_path).read_text(encoding="utf-8"))
        entry = (graph.get("nodes") or {}).get(node) or {}
        lean_file = str(entry.get("lean_file") or "").strip()
        if lean_file:
            return Path(project_dir) / lean_file
    except Exception:  # noqa: BLE001 — resolution is best-effort; the run fails honestly
        pass
    return None


def _sanitize_rel(path_str: str) -> str | None:
    """A model-declared relative path, rejected unless safely inside the project."""
    p = Path(path_str.strip())
    if p.is_absolute() or ".." in p.parts or not str(p).endswith(".lean"):
        return None
    return str(p)


@dataclass
class _ApiRun:
    """Native run state (held inside ``Run.handle``)."""

    node: str
    spec: str
    project_dir: str
    final_text: str = ""
    landed_files: int = 0
    landed_path: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    done: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class OpenAICompatAdapter(ProverAdapter):
    """Drive any OpenAI-compatible chat endpoint as a request/response prover.

    Args:
        graph_path: The plan's graph.json — used to resolve the node's target
            ``lean_file``; a model-declared ``-- FILE: <rel>`` header inside the
            fence is the sanitized fallback.
        preset: ``"openai"`` or ``"avocado"`` — selects default base URL, model
            id, and credential env-var name (each overridable, see module doc).
        base_url / model / key_var / extra_headers: Explicit ctor overrides
            (win over env, which wins over the preset).
        samples: How many completions to request (``n``); the first choice that
            contains a Lean fence (or an honest FAILED) decides the run.
        max_wait_seconds: HTTP timeout for the single blocking call.
        transport: Injectable ``(url, headers, payload, timeout) -> response``
            (tests pass a fake; the default is stdlib urllib).
    """

    name = "openai"
    #: Request/response: no session to steer, no corrective turn to fold into.
    #: A rejected proved-claim downgrades; retry happens a level above.
    steering = SteeringCapability.NONE

    def __init__(
        self,
        *,
        graph_path: str = "",
        preset: str = "openai",
        base_url: str = "",
        model: str = "",
        key_var: str = "",
        extra_headers: dict[str, str] | None = None,
        samples: int = 1,
        max_wait_seconds: float | None = None,
        transport: Any | None = None,
    ) -> None:
        if preset not in _PRESETS:
            raise ValueError(f"unknown preset {preset!r}; expected one of {sorted(_PRESETS)}")
        p_base, p_model, p_keyvar = _PRESETS[preset]
        self.name = preset
        self._graph_path = graph_path
        self._base_url = (base_url or _env(preset, "BASE_URL") or p_base).rstrip("/")
        self._model = model or _env(preset, "MODEL") or p_model
        self._key_var = key_var or _env(preset, "KEY_VAR") or p_keyvar
        raw_headers = _env(preset, "EXTRA_HEADERS")
        self._extra_headers = dict(extra_headers or {})
        if raw_headers:
            try:
                self._extra_headers.update(json.loads(raw_headers))
            except json.JSONDecodeError:
                logger.warning("%s: EXTRA_HEADERS is not valid JSON; ignored", preset)
        self._samples = max(1, int(samples))
        self._timeout = float(max_wait_seconds) if max_wait_seconds else 600.0
        self._transport = transport or _urllib_transport

    # ------------------------------------------------------------------ surface

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        if not self._model:
            raise RuntimeError(
                f"{self.name}: no model configured — set AUTOFORM_"
                f"{self.name.upper()}_MODEL (or pass model=...)"
            )
        if not os.environ.get(self._key_var, "").strip():
            raise RuntimeError(
                f"{self.name}: credential env var {self._key_var!r} is empty — export it "
                f"or point AUTOFORM_{self.name.upper()}_KEY_VAR at the right variable"
            )
        state = _ApiRun(node=node, spec=spec, project_dir=str(project_dir))
        return Run(backend=self.name, goal=spec, project_dir=str(project_dir), handle=state)

    def events(self, run: Run):
        """One blocking completions call, narrated as normalized events."""
        state: _ApiRun = run.handle
        if state.done:  # re-entry (never expected at capability NONE): nothing to add
            return
        state.done = True

        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": self._model,
            "n": self._samples,
            "messages": [
                {"role": "system", "content": SAMPLE_SYSTEM_PROMPT},
                {"role": "user", "content": _build_spec_prompt(state.node, state.spec)},
            ],
        }
        headers = {"Authorization": f"Bearer {os.environ[self._key_var].strip()}",
                   **self._extra_headers}
        yield Event(EventKind.TOOL, f"POST {url} model={self._model} n={self._samples}")
        try:
            resp = self._transport(url, headers, payload, self._timeout)
        except urllib.error.HTTPError as err:
            state.error = f"HTTP {err.code} from {url}: {getattr(err, 'reason', err)}"
            yield Event(EventKind.ERROR, state.error)
            return
        except Exception as err:  # noqa: BLE001 — network layer: fail the run, never the driver
            state.error = f"transport error calling {url}: {err}"
            yield Event(EventKind.ERROR, state.error)
            return

        usage = resp.get("usage") or {}
        state.input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        state.output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

        choices = resp.get("choices") or []
        for choice in choices:
            msg = (choice.get("message") or {})
            text = str(msg.get("content") or "").strip()
            if not text:
                continue
            state.finish_reason = str(choice.get("finish_reason") or "")
            yield Event(EventKind.MESSAGE, text[:2000])
            if _looks_failed(text):
                state.final_text = text
                break
            landed = self._land(state, text)
            if landed is not None:
                state.final_text = text
                yield landed
                break
        if not state.final_text and choices:
            # No candidate produced a fence or an honest FAILED — record the
            # first reply so the failure reason is inspectable.
            state.final_text = str(((choices[0].get("message") or {}).get("content")) or "")
        yield Event(EventKind.RESULT, state.final_text[:2000] if state.final_text
                    else (state.error or "empty response"))

    def steer(self, run: Run, message: str) -> None:
        """No-op by contract: a request/response prover has nothing to steer."""
        logger.info("%s adapter: steer ignored (capability=none)", self.name)

    def result(self, run: Run) -> ProofResult:
        state: _ApiRun = run.handle
        text = (state.final_text or "").strip()
        usage = {"input_tokens": state.input_tokens, "output_tokens": state.output_tokens,
                 "turns": 1 if state.done else 0}
        meta = {"model": self._model, "usage": usage,
                "finish_reason": state.finish_reason, "landed_path": state.landed_path}
        if state.error and not text:
            return ProofResult(status="failed", reason=state.error, backend=self.name,
                               landed_files=0, meta=meta)
        if state.landed_files == 0:
            if _looks_failed(text):
                reason = _failure_reason(text) or "FAILED (no reason given)"
            else:
                reason = state.error or (
                    "no Lean code block in the reply and no honest FAILED line")
            return ProofResult(status="failed", proof_text=text, reason=reason,
                               backend=self.name, landed_files=0, meta=meta)
        proved = not _looks_failed(text)
        return ProofResult(
            status="proved" if proved else "failed",
            proof_text=text,
            reason="" if proved else _failure_reason(text),
            backend=self.name,
            landed_files=state.landed_files,
            meta=meta,
        )

    # ---------------------------------------------------------------- internals

    def _land(self, state: _ApiRun, text: str) -> Event | None:
        """Extract the Lean fence and write it to the node's target file.

        Target resolution: the plan's ``lean_file`` pin wins; else a sanitized
        model-declared ``-- FILE: <rel>`` header inside the fence; else the
        landing fails (and so, honestly, does the run).
        """
        m = _LEAN_FENCE_RE.search(text)
        if not m:
            return None
        content = m.group(1)
        target = _resolve_target_file(self._graph_path, state.node, state.project_dir)
        if target is None:
            header = _FILE_HEADER_RE.match(content)
            rel = _sanitize_rel(header.group(1)) if header else None
            if rel:
                target = Path(state.project_dir) / rel
        if target is None:
            state.error = ("cannot resolve a target file for the landed proof: the plan "
                           "node has no lean_file and the reply declared no -- FILE: header")
            return Event(EventKind.ERROR, state.error)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content if content.endswith("\n") else content + "\n",
                              encoding="utf-8")
        except OSError as err:
            state.error = f"failed to write {target}: {err}"
            return Event(EventKind.ERROR, state.error)
        state.landed_files = 1
        state.landed_path = str(target)
        return Event(EventKind.EDIT, f"landed {target}", path=str(target), payload=content)
