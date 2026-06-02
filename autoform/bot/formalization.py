# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Maintain ``formalization.yaml`` (the mathlib-initiative schema v0.2)
in the workspace's Lean code repo, auto-updating the machine-derivable
fields after each successful merge while preserving every hand-edited
field.

The schema lives at
https://github.com/mathlib-initiative/formalization.yaml/blob/main/formalization.yaml.
Auto-fields are a strict subset of the schema; everything not in
``_AUTO_PATHS`` is treated as human-curated and never overwritten.

Hook point: ``main._on_batch_merged`` calls ``update_formalization`` as
a post-merge step (after the merge eval coroutine is queued). The
update writes to ``<code_path>/formalization.yaml`` and produces a
single follow-on commit on the code repo's main branch. The follow-on
commit pattern (rather than bundling into the merge) matches the
bors-style merge queue's architecture: the merge is already committed
when the callback fires.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FORMALIZATION_FILENAME = "formalization.yaml"
SCHEMA_VERSION = "v0.2"

# --- Schema template (v0.2) ----------------------------------------------
#
# Used when ``formalization.yaml`` doesn't exist yet. Mirrors the
# mathlib-initiative reference verbatim. All fields are present (some
# empty) so a user editing the file sees the full surface.

_TEMPLATE: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "project": {
        "name": "",
        "authors": [],
        "license": "",
    },
    "sources": [],
    "automation": {
        "method": "",
        "models": [],
        "framework": "",
        "cost": {
            "wall_time": "",
            "spend_usd": "",
            "hardware": "",
        },
        "notes": "",
    },
    "status": {
        "scope": "",
        "sorry_count": 0,
        "sorry_in_definitions": 0,
        "axioms": [],
        "main_results": [],
    },
    "fidelity": {
        "divergences": "",
    },
    "review": {
        "status": "",
        "reviewers": [],
        "notes": "",
    },
    "alignment": {},
}

# Paths the auto-updater is allowed to write. Every other key is
# treated as human-curated and preserved verbatim. List items are not
# addressed by this mechanism on purpose (lists are either fully auto
# or fully manual).
_AUTO_PATHS: tuple[tuple[str, ...], ...] = (
    ("version",),
    ("automation", "models"),
    ("automation", "framework"),
    ("status", "sorry_count"),
    ("status", "sorry_in_definitions"),
)


# --- yaml IO --------------------------------------------------------------


def _try_import_yaml():
    """PyYAML is in autoform-bot's deps (see main.py); imported lazily
    so test environments without it surface a clear error rather than
    breaking module import."""
    try:
        import yaml  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "formalization.yaml support requires PyYAML; "
            "this is already a top-level autoform-bot dep, so the "
            "import failure here likely means an environment issue."
        ) from e
    return yaml


def read_formalization(path: Path) -> dict[str, Any]:
    """Load ``formalization.yaml`` or return a fresh template."""
    if not path.is_file():
        return _deep_copy(_TEMPLATE)
    yaml = _try_import_yaml()
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        logger.warning(
            "formalization.yaml at %s is malformed (%s); starting from template",
            path, e,
        )
        return _deep_copy(_TEMPLATE)
    out = _deep_copy(_TEMPLATE)
    _deep_merge(out, loaded)
    return out


def write_formalization(path: Path, data: dict[str, Any]) -> None:
    """Write with deterministic key order (matches template) — minimizes
    diff noise across runs.

    Multi-line strings are emitted as literal block scalars (``|-``)
    rather than the default expanded-plain style, which preserves
    paragraph structure for the human-curated ``notes`` / ``scope`` /
    ``divergences`` fields.
    """
    yaml = _try_import_yaml()
    ordered = _reorder_to_template(data)

    def _str_representer(dumper, value: str):
        if "\n" in value:
            return dumper.represent_scalar(
                "tag:yaml.org,2002:str", value, style="|"
            )
        return dumper.represent_scalar("tag:yaml.org,2002:str", value)

    dumper = yaml.SafeDumper
    dumper.add_representer(str, _str_representer)
    text = yaml.dump(
        ordered,
        Dumper=dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10000,
    )
    path.write_text(text)


# --- auto-field computation ----------------------------------------------


def count_sorries(code_dir: Path) -> tuple[int, int]:
    """Count ``sorry`` occurrences across all ``.lean`` files under
    ``code_dir`` (gitignore-filtered).

    Returns ``(total_sorries, sorries_in_definitions)``. The second
    is an approximation: counts sorries whose nearest preceding
    declaration keyword is def/abbrev/instance/structure/class/inductive
    (the declaration-form decls), as opposed to theorem/lemma (the
    proof-form decls). False positives possible for sorries inside
    ``where`` clauses or ``let rec``; a precise count needs
    ``#print axioms`` on a built repo (autoform-bot's eval pipeline
    already runs that — this auto-field is the cheap proxy that
    refreshes per merge).
    """
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=str(code_dir),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return 0, 0
    total = 0
    in_defs = 0
    decl_re = re.compile(
        r"^\s*(?:@\[[^\]]*\]\s*)*(?:noncomputable\s+)?"
        r"(?:private\s+)?(?:protected\s+)?"
        r"(def|theorem|lemma|abbrev|instance|structure|class|inductive|opaque|axiom)\b"
    )
    sorry_re = re.compile(r"\bsorry\b")
    def_kinds = {"def", "abbrev", "instance", "structure", "class", "inductive"}
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8")
        if not rel.endswith(".lean"):
            continue
        full = code_dir / rel
        if not full.is_file():
            continue
        try:
            lines = full.read_text().splitlines()
        except OSError:
            continue
        current_kind: str | None = None
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("--"):
                continue
            m = decl_re.match(line)
            if m:
                current_kind = m.group(1)
            n = len(sorry_re.findall(line))
            if n:
                total += n
                if current_kind in def_kinds:
                    in_defs += n
    return total, in_defs


def compute_auto_fields(
    code_dir: Path,
    models: list[str] | None = None,
    framework: str | None = None,
) -> dict[str, Any]:
    """Compute the auto-field subset that ``update_formalization`` overlays
    onto the read-in dict."""
    total, in_defs = count_sorries(code_dir)
    out: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "status": {
            "sorry_count": total,
            "sorry_in_definitions": in_defs,
        },
        "automation": {},
    }
    if models is not None:
        out["automation"]["models"] = list(models)
    if framework is not None:
        out["automation"]["framework"] = framework
    return out


# --- axiom checking ------------------------------------------------------
#
# ``status.main_results[].axioms`` is the per-declaration axiom set
# Mathlib's ``#print axioms`` reports. Auto-checking requires the
# workspace to be built (.olean files present, which is true after a
# successful merge-queue lake build). The checker batches every
# main-result decl into one Lean invocation to avoid N process spawns
# per merge.


def _module_from_file_path(file_path: str) -> str | None:
    """Convert a source path (``GeometricAnalysis/LeeSM/Chapter16/
    StokesTheorem.lean``) to a Lean module (``GeometricAnalysis.LeeSM.
    Chapter16.StokesTheorem``). Returns ``None`` for non-Lean /
    absolute / empty inputs."""
    if not file_path or not file_path.endswith(".lean"):
        return None
    if file_path.startswith("/"):
        return None
    stem = file_path[: -len(".lean")]
    return stem.replace("/", ".")


_AXIOMS_LIST_RE = re.compile(
    r"'(?P<decl>[^']+)' depends on axioms:\s*\[(?P<axioms>.*?)\]",
    re.DOTALL,
)
_AXIOMS_NONE_RE = re.compile(
    r"'(?P<decl>[^']+)' does not depend on any axioms"
)


def check_axioms(
    code_dir: Path,
    decl_to_module: list[tuple[str, str]],
    *,
    timeout: int = 120,
) -> dict[str, list[str] | None]:
    """Run ``#print axioms`` on a batch of declarations in one Lean
    invocation. Returns ``{decl_name: axioms | None}``; ``None`` means
    the declaration's axioms couldn't be determined (build missing,
    decl not in scope, timeout). Decls that genuinely depend on no
    axioms return an empty list.

    Architecturally distinct from autoform-bot's existing
    ``autoform.eval.lean_checks.AxiomsChecker``: this one runs in the
    merge-queue post-hook (cheap, batched, async to the eval loop);
    that one runs in the per-statement eval phase (precise,
    integrated with the matching agent). They share the parsing
    shape but not the call surface.
    """
    if not decl_to_module:
        return {}
    modules_seen: set[str] = set()
    modules_ordered: list[str] = []
    for _, mod in decl_to_module:
        if mod not in modules_seen:
            modules_seen.add(mod)
            modules_ordered.append(mod)
    lines = [f"import {m}" for m in modules_ordered]
    lines.append("")
    for decl, _ in decl_to_module:
        lines.append(f"#print axioms {decl}")
    tmp_path = code_dir / ".autoform-axioms-check.lean"
    out: dict[str, list[str] | None] = {decl: None for decl, _ in decl_to_module}
    try:
        tmp_path.write_text("\n".join(lines) + "\n")
        try:
            proc = subprocess.run(
                ["lake", "env", "lean", str(tmp_path.name)],
                cwd=str(code_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "axiom check timed out after %ds for %d decls",
                timeout, len(decl_to_module),
            )
            return out
        text = proc.stdout
        for m in _AXIOMS_LIST_RE.finditer(text):
            decl = m.group("decl")
            ax_blob = m.group("axioms")
            axs = [a.strip() for a in ax_blob.split(",") if a.strip()]
            out[decl] = axs
        for m in _AXIOMS_NONE_RE.finditer(text):
            out[m.group("decl")] = []
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return out


def update_main_results_axioms(
    data: dict[str, Any], code_dir: Path
) -> int:
    """Walk ``data['status']['main_results']`` and replace each entry's
    ``axioms`` list with the verified set. Returns the count of
    updated entries; entries whose axioms can't be determined are
    left unchanged."""
    main_results = (data.get("status") or {}).get("main_results") or []
    decl_to_module: list[tuple[str, str]] = []
    for entry in main_results:
        decl = entry.get("declaration") if isinstance(entry, dict) else None
        file_path = entry.get("file") if isinstance(entry, dict) else None
        if not decl or not file_path:
            continue
        module = _module_from_file_path(file_path)
        if module is None:
            continue
        decl_to_module.append((decl, module))
    if not decl_to_module:
        return 0
    axioms_by_decl = check_axioms(code_dir, decl_to_module)
    updated = 0
    for entry in main_results:
        if not isinstance(entry, dict):
            continue
        decl = entry.get("declaration")
        result = axioms_by_decl.get(decl)
        if result is None:
            continue
        entry["axioms"] = result
        updated += 1
    return updated


# --- orchestrator + auto-commit ------------------------------------------


def update_formalization(
    code_dir: Path,
    models: list[str] | None = None,
    framework: str | None = None,
    yaml_path: Path | None = None,
    create_if_missing: bool = False,
    commit: bool = True,
    commit_message: str | None = None,
    check_axioms_on_build: bool = False,
) -> Path | None:
    """Read, update the auto-fields, write back, optionally commit.

    Returns the path written, or ``None`` if the file was absent and
    ``create_if_missing`` was False, or if the file didn't change.

    Args:
        code_dir: Workspace's Lean code repo root (``<run_dir>/code``).
        models: Model identifiers to stamp into ``automation.models``.
        framework: ``"autoform-bot"`` by default at the merge-hook
            call site; override if the consumer pipes autoform-bot
            through a different harness.
        yaml_path: Override file location. Defaults to
            ``code_dir / formalization.yaml``.
        create_if_missing: If True, create from template when absent.
            Default False — opt-in per project, so the workspace
            doesn't accumulate a file the maintainer didn't ask for.
            Initialize via ``autoform formalization-init`` (CLI).
        commit: If True, commit the change to the code repo with
            ``commit_message`` (or a sensible default). Default True
            — the merge-hook fires on a clean tree, so the follow-on
            commit only stages formalization.yaml.
        commit_message: Override the default commit message. Default
            is ``"formalization: auto-refresh after merge"``.
        check_axioms_on_build: If True, run ``#print axioms`` on every
            declaration in ``status.main_results`` and replace their
            ``axioms`` lists with the verified set. Costs one
            ``lake env lean`` invocation (batched across all main
            results). Safe to enable at the merge-hook call site —
            the merge queue's `lake build` gate guarantees the build
            is current. Default False.
    """
    yaml_path = yaml_path or (code_dir / FORMALIZATION_FILENAME)
    if not yaml_path.is_file() and not create_if_missing:
        return None

    current = read_formalization(yaml_path)
    auto = compute_auto_fields(code_dir, models=models, framework=framework)
    merged = _overlay_auto_fields(current, auto)
    if check_axioms_on_build:
        try:
            updated = update_main_results_axioms(merged, code_dir)
            if updated:
                logger.info(
                    "formalization: refreshed axioms for %d main_result(s)",
                    updated,
                )
        except Exception:  # noqa: BLE001 — soft-warning
            logger.exception(
                "formalization: axiom check failed; existing axioms preserved"
            )

    # Detect no-op: serialize candidate + on-disk, compare. Skips both
    # the disk write and the follow-on commit when nothing changed.
    yaml = _try_import_yaml()
    new_text = yaml.safe_dump(
        _reorder_to_template(merged),
        sort_keys=False, default_flow_style=False,
        allow_unicode=True, width=10000,
    )
    existing_text = yaml_path.read_text() if yaml_path.is_file() else ""
    # Strip the _auto last-updated stamp before comparison — otherwise
    # every iteration would commit just the timestamp.
    if _strip_auto_stamp(new_text) == _strip_auto_stamp(existing_text):
        return None

    yaml_path.write_text(new_text)

    if commit:
        try:
            _commit_change(code_dir, yaml_path, commit_message)
        except Exception:  # noqa: BLE001
            logger.exception(
                "formalization.yaml refresh wrote %s but the follow-on "
                "git commit failed; the change is still on disk and "
                "will land with the next git commit on this repo.",
                yaml_path,
            )

    return yaml_path


def _commit_change(
    code_dir: Path, yaml_path: Path, message: str | None
) -> None:
    """Stage + commit only ``yaml_path`` on the code repo. Skips
    silently if there's nothing to commit (the merge queue may have
    raced us)."""
    try:
        rel = yaml_path.relative_to(code_dir)
    except ValueError:
        rel = yaml_path
    add = subprocess.run(
        ["git", "add", "--", str(rel)],
        cwd=str(code_dir), capture_output=True, text=True, check=False,
    )
    if add.returncode != 0:
        raise RuntimeError(
            f"git add failed: {(add.stderr or add.stdout).strip()}"
        )
    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(code_dir), capture_output=True, check=False,
    )
    if diff_check.returncode == 0:
        return  # nothing staged
    msg = message or (
        "formalization: auto-refresh after merge\n\n"
        "Updated by autoform-bot's post-merge hook. Auto-fields:\n"
        "version, automation.{models,framework}, "
        "status.{sorry_count,sorry_in_definitions}. All other "
        "fields preserved verbatim from the on-disk file."
    )
    proc = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(code_dir), capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git commit failed: {(proc.stderr or proc.stdout).strip()}"
        )


# --- private helpers -----------------------------------------------------


def _deep_copy(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _deep_copy(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_deep_copy(v) for v in d]
    return d


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = _deep_copy(v)


def _overlay_auto_fields(
    base: dict[str, Any], auto: dict[str, Any]
) -> dict[str, Any]:
    out = _deep_copy(base)
    for path in _AUTO_PATHS:
        node: Any = auto
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if node is None:
            continue
        cursor = out
        for key in path[:-1]:
            cursor = cursor.setdefault(key, {})
            if not isinstance(cursor, dict):
                logger.warning(
                    "formalization.yaml: path %s blocked by non-dict; "
                    "skipping auto-update", ".".join(path)
                )
                cursor = None
                break
        if cursor is None:
            continue
        cursor[path[-1]] = node
    _stamp_last_updated(out)
    return out


_LAST_UPDATED_PREFIX = "_auto: last updated by autoform-bot at "


def _stamp_last_updated(data: dict[str, Any]) -> None:
    autom = data.setdefault("automation", {})
    existing = autom.get("notes", "") or ""
    cleaned = "\n".join(
        line for line in existing.splitlines()
        if not line.startswith(_LAST_UPDATED_PREFIX)
    ).rstrip()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_line = f"{_LAST_UPDATED_PREFIX}{ts}"
    autom["notes"] = (cleaned + ("\n" if cleaned else "") + new_line).strip()


_AUTO_STAMP_LINE_RE = re.compile(
    r"\n\s*_auto: last updated by autoform-bot at [^\n]+", re.MULTILINE
)


def _strip_auto_stamp(text: str) -> str:
    """Remove the ``_auto:`` stamp line for content-equality comparison.
    The stamp updates every call, so without stripping every refresh
    would produce a no-content-change commit."""
    return _AUTO_STAMP_LINE_RE.sub("", text)


def _reorder_to_template(data: dict[str, Any]) -> dict[str, Any]:
    return _reorder_section(data, _TEMPLATE)


def _reorder_section(data: Any, template: Any) -> Any:
    if not isinstance(data, dict) or not isinstance(template, dict):
        return data
    out: dict[str, Any] = {}
    for k in template:
        if k in data:
            out[k] = _reorder_section(data[k], template[k])
    for k, v in data.items():
        if k not in out:
            out[k] = v
    return out
