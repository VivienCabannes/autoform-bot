#!/usr/bin/env python3
"""formalization.yaml — create and maintain the mathlib-initiative manifest.

The community standard (github.com/mathlib-initiative/formalization.yaml, v0.3)
is a *self-reporting* manifest at the Lean project root: provenance, sources,
automation methods, cost, status, review. This module gives autoform two verbs::

    python3 scripts/formalization.py init   <project_dir> [--name N] [--force] ...
    python3 scripts/formalization.py update <project_dir> [--no-commit]

and a library surface (``record_run`` / ``update_formalization``) the prover
server calls after every run, so the file stays accurate without a human
babysitting it.

DESIGN (ported from autoform-bot's earlier ``autoform/bot/formalization.py``
prior art, adapted to schema v0.3):

* **Machine/human split by allowlist.** The machine writes ONLY: ``version``,
  ``status.sorry_count``, ``status.sorry_in_definitions``, and the single entry
  of ``automation.methods`` whose ``framework`` is ``"autoform"`` (created when
  missing). Everything else — project identity, sources, other methods, review,
  fidelity, alignment — is human-curated and round-trips verbatim, unknown keys
  included ("Readers should tolerate unknown keys", v0.3 header).
* **The ledger is the source of truth; the yaml is a derived view.** Every
  prover run appends one JSON line to ``<project>/.autoform/usage.jsonl``
  (see :func:`record_run` — token counts, notional cost, wall seconds, backend,
  model, judge usage). ``update`` folds the ledger into the autoform method's
  ``models`` / ``cost`` / ``tokens`` — so a re-init or a hand-deleted yaml can
  always be reconstructed accurately from the ledger.
* **Tokens.** The v0.3 spec has NO token fields (cost stops at per-method
  ``wall_time``/``spend_usd``/``hardware``); the spec explicitly tolerates
  extra keys, so token totals live in an extra ``tokens:`` mapping inside the
  autoform method entry — honest, machine-owned, ignorable by other readers.
* **No-op detection** (with the timestamp stripped from the comparison) so a
  refresh with no content change writes nothing; atomic tmp+rename writes;
  template-ordered output with ``|-`` block scalars for human prose.

Stdlib + PyYAML only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = "v0.3"
AUTOFORM_FRAMEWORK = "autoform"
LEDGER_RELPATH = Path(".autoform") / "usage.jsonl"
_STAMP_PREFIX = "_auto: last updated by autoform at "

# --------------------------------------------------------------------------- template

#: The v0.3 surface, present-but-empty so a human sees every field. Hard schema
#: requirements are only: project{name,authors,license}, sources[≥1]{title,
#: authors,id}, automation.methods[≥1]{method}, review.status — all non-blank.
_TEMPLATE: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "project": {"name": "", "authors": [], "license": ""},
    "sources": [],
    "automation": {
        "methods": [],
        "spend_usd": "",
        "notes": "",
    },
    "status": {
        "scope": "",
        "sorry_count": 0,
        "sorry_in_definitions": 0,
        "axioms": [],
        "main_results": [],
    },
    "fidelity": {"divergences": ""},
    "review": {"status": "unchecked", "reviewers": [], "notes": ""},
    "alignment": {},
    "acknowledgements": "",
}


def _fresh_autoform_method() -> dict[str, Any]:
    """The machine-owned ``automation.methods`` entry, before ledger rollup."""
    return {
        "method": "agent",
        "models": [],
        "framework": AUTOFORM_FRAMEWORK,
        "tool_setup": (
            "autoform Claude Code plugin: plan graph -> prover backends "
            "(claude/aristotle/codex/openai) -> kernel honesty gate -> review jury"
        ),
        "cost": {"wall_time": "", "spend_usd": "", "hardware": ""},
        # Extra key (schema-tolerated): machine-accurate token accounting the
        # spec itself has no fields for.
        "tokens": {"input": 0, "output": 0, "runs": 0},
        "notes": "",
    }


# --------------------------------------------------------------------------- yaml io


class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_representer)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """``overlay`` over ``base``: dicts merge recursively, everything else replaces."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _reorder_to_template(data: dict, template: dict) -> dict:
    """Template key order first, unknown keys appended — diff-stable output."""
    out: dict[str, Any] = {}
    for k in template:
        if k in data:
            v = data[k]
            t = template[k]
            out[k] = _reorder_to_template(v, t) if isinstance(v, dict) and isinstance(t, dict) else v
    for k, v in data.items():
        if k not in out:
            out[k] = v
    return out


def yaml_path_for(project_dir: str | Path) -> Path:
    return Path(project_dir) / "formalization.yaml"


def read_formalization(project_dir: str | Path) -> dict[str, Any] | None:
    """Load the manifest merged over the template; ``None`` when absent/unreadable."""
    path = yaml_path_for(project_dir)
    if not path.exists():
        return None
    try:
        on_disk = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(on_disk, dict):
        return None
    return _deep_merge(_TEMPLATE, on_disk)


def write_formalization(project_dir: str | Path, data: dict[str, Any]) -> None:
    """Atomic, template-ordered write (tmp + rename in the target dir)."""
    path = yaml_path_for(project_dir)
    ordered = _reorder_to_template(data, _TEMPLATE)
    text = yaml.dump(ordered, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=88)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".formalization.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- ledger


def ledger_path_for(project_dir: str | Path) -> Path:
    return Path(project_dir) / LEDGER_RELPATH


@contextlib.contextmanager
def _usage_lock(project_dir: str | Path):
    """Serialize ledger appends and derived-manifest refreshes per project."""
    state_dir = Path(project_dir) / ".autoform"
    if state_dir.is_symlink():
        raise ValueError(f"usage state directory is a symlink: {state_dir}")
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "usage.lock"
    if lock_path.is_symlink():
        raise ValueError(f"usage lock is a symlink: {lock_path}")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def record_run(project_dir: str | Path, entry: dict[str, Any]) -> Path:
    """Append one run's usage record to the project ledger (JSONL, append-only).

    ``entry`` is whatever the prover server assembled ({ts, node, backend,
    model, status, wall_seconds, usage…}); this function only guarantees the
    directory exists and the line is one valid JSON object.
    """
    path = ledger_path_for(project_dir)
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _usage_lock(project_dir):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return path


def read_ledger(project_dir: str | Path) -> list[dict[str, Any]]:
    """All parseable ledger entries (a torn trailing line is skipped)."""
    path = ledger_path_for(project_dir)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _format_wall_time(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return "<1m" if s else ""
    days, rem = divmod(s, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _rollup(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold the ledger into the autoform method's machine fields."""
    models: set[str] = set()
    tokens_in = tokens_out = 0
    wall = 0.0
    notional = 0.0                       # subscription runs only: what the API WOULD have cost
    subscription_runs = api_runs = external_runs = 0
    for e in entries:
        model = str(e.get("model") or "").strip()
        backend = str(e.get("backend") or "").strip()
        if backend == "aristotle":
            models.add("Aristotle")
        elif model:
            models.add(model)
        wall += float(e.get("wall_seconds") or 0.0)
        billing = str(e.get("billing") or "")
        usage = e.get("usage") or {}
        for part in ("worker", "judge"):
            u = usage.get(part) or {}
            tokens_in += int(u.get("input_tokens") or 0)
            tokens_out += int(u.get("output_tokens") or 0)
            if billing == "subscription":
                notional += float(u.get("cost_usd") or 0.0)
        if billing == "api":
            api_runs += 1
        elif billing == "subscription":
            subscription_runs += 1
        else:
            external_runs += 1           # aristotle compute, codex's own auth, unknown
    spend_parts = []
    if subscription_runs:
        note = f"subscription-based usage ({subscription_runs} runs"
        if notional:
            note += f"; notional API-equivalent ${notional:.2f}"
        note += ")"
        spend_parts.append(note)
    if api_runs:
        spend_parts.append(f"API-billed runs: {api_runs} (see .autoform/usage.jsonl)")
    if external_runs:
        spend_parts.append(f"externally-billed runs: {external_runs} "
                           "(Aristotle compute / other-vendor auth)")
    return {
        "models": sorted(models),
        "wall_time": _format_wall_time(wall),
        "spend_usd": "; ".join(spend_parts),
        "tokens": {"input": tokens_in, "output": tokens_out, "runs": len(entries)},
    }


# --------------------------------------------------------------------------- sorries


_DECL_RE = re.compile(
    r"\b(theorem|lemma|example|def|abbrev|instance|structure|class|inductive)\b")
_SORRY_RE = re.compile(r"\bsorry\b")
_DEF_KEYWORDS = {"def", "abbrev", "instance", "structure", "class", "inductive"}


def _strip_lean_noise(src: str) -> str:
    """Blank out comments and string literals, preserving offsets.

    A single-pass scanner rather than regexes: Lean block comments NEST, a
    ``--`` line comment may *mention* ``/-`` (a regex approach opens a phantom
    block there and swallows real code — undercounting sorries, the dangerous
    direction), and a ``sorry`` inside a string literal is prose, not a proof
    hole. Replaced spans become spaces so positions stay comparable.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":                     # line comment
            j = src.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif ch == "/" and nxt == "-":                   # nested block comment
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/-", j):
                    depth += 1
                    j += 2
                elif src.startswith("-/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif ch == '"':                                  # string literal
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            for k in range(i, j):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def count_sorries(project_dir: str | Path) -> tuple[int, int]:
    """(total sorries, sorries under definition-forming decls) — a cheap proxy.

    Comment-stripped regex scan over the project's ``.lean`` files (git-tracked
    plus untracked, falling back to rglob outside a git repo), attributing each
    ``sorry`` to the nearest preceding declaration keyword. The precise ground
    truth stays ``#print axioms`` in the verify gate; this is the per-refresh
    approximation the manifest's status block wants.
    """
    root = Path(project_dir)
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.splitlines()
        files = [root / f for f in listed if f.endswith(".lean")]
    except Exception:
        files = [p for p in root.rglob("*.lean") if ".lake" not in p.parts]
    total = in_defs = 0
    for f in files:
        if ".lake" in f.parts:
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        src = _strip_lean_noise(src)
        last_decl = ""
        events = sorted(
            [(m.start(), "decl", m.group(1)) for m in _DECL_RE.finditer(src)]
            + [(m.start(), "sorry", "") for m in _SORRY_RE.finditer(src)]
        )
        for _start, kind, word in events:
            if kind == "decl":
                last_decl = word
            else:
                total += 1
                if last_decl in _DEF_KEYWORDS:
                    in_defs += 1
    return total, in_defs


# --------------------------------------------------------------------------- update


def _autoform_method(data: dict[str, Any]) -> dict[str, Any]:
    """The machine-owned method entry (created and appended when missing).

    Tolerant of hand-mangled shapes: a human who typed a scalar where the
    schema wants a mapping/list gets their value pushed aside (into the shape
    the machine needs) rather than a traceback — "human fields tolerated"
    must include human mistakes in machine-adjacent paths.
    """
    if not isinstance(data.get("automation"), dict):
        data["automation"] = {"methods": [], "spend_usd": "", "notes": ""}
    automation = data["automation"]
    if not isinstance(automation.get("methods"), list):
        automation["methods"] = []
    for entry in automation["methods"]:
        if isinstance(entry, dict) and entry.get("framework") == AUTOFORM_FRAMEWORK:
            if not isinstance(entry.get("cost"), dict):
                entry["cost"] = {"wall_time": "", "spend_usd": "", "hardware": ""}
            return entry
    entry = _fresh_autoform_method()
    automation["methods"].append(entry)
    return entry


def _stamp(entry: dict[str, Any]) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    notes = str(entry.get("notes") or "")
    # Replace (never accumulate) the stamp line; preserve the human's other
    # lines verbatim, blank lines included.
    lines = [ln for ln in notes.splitlines() if not ln.startswith(_STAMP_PREFIX)]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(_STAMP_PREFIX + ts)
    entry["notes"] = "\n".join(lines).strip("\n")


def _strip_stamp(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if _STAMP_PREFIX not in ln)


def _update_formalization_unlocked(
    project_dir: str | Path,
    *,
    count_lean_sorries: bool = True,
    create_if_missing: bool = False,
) -> Path | None:
    """Refresh the machine-owned fields from the ledger; no-op when unchanged.

    Returns the yaml path when written, ``None`` when absent (and not created)
    or when the refresh produced no content change.
    """
    data = read_formalization(project_dir)
    if data is None:
        if not create_if_missing:
            return None
        data = json.loads(json.dumps(_TEMPLATE))  # deep copy

    data["version"] = SCHEMA_VERSION
    entry = _autoform_method(data)
    roll = _rollup(read_ledger(project_dir))
    entry["models"] = roll["models"]
    entry.setdefault("cost", {})
    entry["cost"]["wall_time"] = roll["wall_time"]
    entry["cost"]["spend_usd"] = roll["spend_usd"]
    entry["tokens"] = roll["tokens"]
    if count_lean_sorries:
        total, in_defs = count_sorries(project_dir)
        if not isinstance(data.get("status"), dict):
            data["status"] = {}      # a hand-typed scalar: machine counts still land
        data["status"]["sorry_count"] = total
        data["status"]["sorry_in_definitions"] = in_defs
    _stamp(entry)

    path = yaml_path_for(project_dir)
    candidate = yaml.dump(_reorder_to_template(data, _TEMPLATE), Dumper=_Dumper,
                          sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=88)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if _strip_stamp(current) == _strip_stamp(candidate):
            return None  # timestamp-only change: skip the write entirely
    write_formalization(project_dir, data)
    return path


def update_formalization(project_dir: str | Path, *, count_lean_sorries: bool = True,
                         create_if_missing: bool = False) -> Path | None:
    """Refresh the derived manifest while excluding concurrent ledger writers."""
    with _usage_lock(project_dir):
        return _update_formalization_unlocked(
            project_dir,
            count_lean_sorries=count_lean_sorries,
            create_if_missing=create_if_missing,
        )


# --------------------------------------------------------------------------- init


def _git_author(project_dir: str | Path) -> str:
    try:
        out = subprocess.run(["git", "config", "user.name"], cwd=str(project_dir),
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception:
        return ""


def init_formalization(project_dir: str | Path, *, name: str = "", authors: list[str] | None = None,
                       license_id: str = "Apache-2.0", force: bool = False) -> Path:
    """Create a schema-valid seed manifest (refusing to overwrite without force).

    Seeds the hard-required fields with real values where derivable (project
    name from the directory, author from git config) and clearly-marked TODO
    placeholders elsewhere (the schema rejects blank strings). Any existing
    ledger is rolled up immediately, so a late init backfills accurate totals.
    """
    root = Path(project_dir)
    path = yaml_path_for(root)
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists (use --force to overwrite)")

    data = json.loads(json.dumps(_TEMPLATE))  # deep copy
    data["project"]["name"] = name or root.resolve().name
    data["project"]["authors"] = authors or [_git_author(root) or "TODO: author"]
    data["project"]["license"] = license_id
    data["sources"] = [{
        "title": "TODO: primary source (textbook/paper/blueprint)",
        "authors": ["TODO"],
        "id": "TODO: DOI, ISBN, arXiv id, or URL",
        "type": "textbook",
        "license": "",
        "author_contacted": "n/a",
    }]
    data["automation"]["methods"] = [_fresh_autoform_method()]
    data["review"]["status"] = "unchecked"
    write_formalization(root, data)
    # Fold in any pre-existing ledger + live sorry counts right away.
    update_formalization(root, create_if_missing=False)
    return path


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="verb", required=True)

    p_init = sub.add_parser("init", help="create formalization.yaml (opt-in, refuses overwrite)")
    p_init.add_argument("project_dir")
    p_init.add_argument("--name", default="")
    p_init.add_argument("--author", action="append", default=[])
    p_init.add_argument("--license", dest="license_id", default="Apache-2.0")
    p_init.add_argument("--force", action="store_true")

    p_up = sub.add_parser("update", help="refresh machine fields from the usage ledger")
    p_up.add_argument("project_dir")
    p_up.add_argument("--no-sorries", action="store_true",
                      help="skip the .lean sorry scan (fast path)")

    args = ap.parse_args(argv)
    if args.verb == "init":
        try:
            path = init_formalization(args.project_dir, name=args.name,
                                      authors=args.author or None,
                                      license_id=args.license_id, force=args.force)
        except FileExistsError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1
        print(f"created {path}")
        return 0
    if args.verb == "update":
        path = update_formalization(args.project_dir,
                                    count_lean_sorries=not args.no_sorries)
        print(f"updated {path}" if path else "no change (or no formalization.yaml — run init)")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
