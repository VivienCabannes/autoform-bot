# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Build the raw dependency graph by running the Lean metaprogram."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path

from .lean_script import LEAN_SCRIPT
from .types import AUTO_GENERATED_SUFFIXES, GraphNode

logger = logging.getLogger(__name__)

_TIMEOUT = 3600  # 1 hour — loading large environments can be slow


def _scan_unproved_names(repo_dir: Path) -> tuple[bool, set[str]]:
    """Scan source files for ``import Unproved`` and ``unproved`` declarations.

    Returns (has_unproved_import, declaration_names).  For each declaration
    both the captured name *and* its last dot-component are stored so that
    ``_mark_unproved`` can match against fully-qualified graph node names.
    Single pass over all .lean files.
    """
    has_import = False
    names: set[str] = set()
    for f in repo_dir.rglob("*.lean"):
        if ".lake" in str(f) or f.name.startswith("_dep_graph_"):
            continue
        try:
            content = f.read_text(errors="replace")
        except OSError:
            continue
        if "import Unproved" in content:
            has_import = True
        # Match: `unproved TheName` — the macro only works with a name, not keywords
        for m in re.finditer(r"^\s*unproved\s+([A-Za-z_]\w*(?:\.\w+)*)", content, re.MULTILINE):
            captured = m.group(1)
            names.add(captured)
            # Also store the unqualified (last-component) name for matching
            if "." in captured:
                names.add(captured.rsplit(".", 1)[-1])
        for m in re.finditer(r"@\[unproved\]\s*axiom\s+([A-Za-z_]\w*(?:\.\w+)*)", content, re.MULTILINE):
            captured = m.group(1)
            names.add(captured)
            if "." in captured:
                names.add(captured.rsplit(".", 1)[-1])
    return has_import, names


async def build_raw_graph(
    repo_dir: Path,
    module_prefix: str,
    import_module: str | None = None,
    timeout: float = _TIMEOUT,
) -> dict[str, GraphNode]:
    """Run the Lean metaprogram and parse output into GraphNode dicts.

    Args:
        repo_dir: Path to the Lean repository root (where lakefile.toml lives).
        module_prefix: Module name prefix for project-local declarations.
        import_module: Module to import. Defaults to module_prefix.
        timeout: Max seconds for the Lean process.

    Returns:
        Dict mapping declaration names to GraphNode instances.
    """
    import_mod = import_module or module_prefix
    script = LEAN_SCRIPT.replace("{import_module}", import_mod).replace("{module_prefix}", module_prefix)

    # Single pass: detect import Unproved + collect unproved declaration names
    has_unproved_import, unproved_names = _scan_unproved_names(repo_dir)

    if has_unproved_import:
        script = script.replace("{unproved_check}", "unprovedAttr.hasTag env name")
        # The metaprogram needs Unproved imported explicitly to access unprovedAttr
        script = "import Unproved\n" + script
    else:
        script = script.replace("{unproved_check}", "false")

    tmp_file = repo_dir / f"_dep_graph_{uuid.uuid4().hex[:8]}.lean"
    tmp_file.write_text(script, encoding="utf-8")

    try:
        logger.info("Running lean metaprogram (this loads the full environment, may take a few minutes)...")
        proc = await asyncio.create_subprocess_exec(
            "lake",
            "env",
            "lean",
            str(tmp_file),
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"Dependency graph timed out after {timeout}s")

        output = stdout.decode().strip()
        err = stderr.decode().strip()
        logger.info("Lean metaprogram finished (rc=%d), parsing output...", proc.returncode)

        if proc.returncode != 0:
            # Try to parse output anyway — #eval! may produce warnings
            # but still output valid data
            nodes = _parse_output(output)
            if nodes:
                logger.warning(
                    "Dependency graph had warnings (rc=%d) but produced %d nodes",
                    proc.returncode,
                    len(nodes),
                )
                return _mark_unproved(nodes, unproved_names)
            raise RuntimeError(f"Dependency graph failed (rc={proc.returncode}):\n{err}\n{output[:1000]}")

        return _mark_unproved(_parse_output(output), unproved_names)

    finally:
        tmp_file.unlink(missing_ok=True)


def _parse_output(output: str) -> dict[str, GraphNode]:
    """Parse pipe-delimited Lean output into GraphNode dicts.

    Format: NAME|KIND|IS_CLASS|TYPE_HEAD|HAS_SORRY|BODY_TAGS|DEP1,DEP2,...|FIELD_DEP1,FIELD_DEP2,...|IS_UNPROVED

    Instance counts and is_auto_generated are computed in Python.
    Field deps come from the Lean script (via env.isProjectionFn).
    """
    # First pass: parse raw data
    raw: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        raw.append(
            {
                "name": parts[0],
                "kind": parts[1],
                "is_class": parts[2] == "true",
                "type_head": parts[3],
                "has_sorry": parts[4] == "true",
                "body_tags": tuple(t.strip() for t in parts[5].split(";") if t.strip()),
                "deps_str": parts[6],
                "field_deps_str": parts[7] if len(parts) > 7 else "",
                "is_unproved": parts[8] == "true" if len(parts) > 8 else False,
            }
        )

    if not raw:
        logger.warning(
            "No declarations found in dependency graph output:\n%s",
            output[:500],
        )
        return {}

    # Collect all project class names
    class_names = {r["name"] for r in raw if r["is_class"]}

    # Count instances per class: declarations whose type_head is a class name
    # Exclude auto-generated declarations
    instance_counts: dict[str, int] = {name: 0 for name in class_names}
    for r in raw:
        head = r["type_head"]
        if head in class_names and not any(r["name"].endswith(s) for s in AUTO_GENERATED_SUFFIXES):
            instance_counts[head] = instance_counts.get(head, 0) + 1

    # Build nodes
    nodes: dict[str, GraphNode] = {}
    for r in raw:
        deps = tuple(d.strip() for d in r["deps_str"].split(",") if d.strip()) if r["deps_str"].strip() else ()

        # Field deps from Lean (via env.isProjectionFn)
        field_deps = (
            tuple(d.strip() for d in r["field_deps_str"].split(",") if d.strip()) if r["field_deps_str"].strip() else ()
        )

        # is_auto_generated: name ends with an auto-generated suffix
        is_auto = any(r["name"].endswith(s) for s in AUTO_GENERATED_SUFFIXES)

        nodes[r["name"]] = GraphNode(
            name=r["name"],
            kind=r["kind"],
            is_class=r["is_class"],
            is_auto_generated=is_auto,
            has_sorry=r["has_sorry"],
            is_unproved=r["is_unproved"],
            instance_count=instance_counts.get(r["name"], 0),
            type_head=r["type_head"],
            deps=deps,
            field_deps=field_deps,
            tags=r["body_tags"],  # body-level tags from Lean; graph-level tags added later
        )

    return nodes


def _mark_unproved(nodes: dict[str, GraphNode], unproved_names: set[str]) -> dict[str, GraphNode]:
    """Ensure axioms declared with ``unproved`` are marked ``is_unproved``.

    The Lean metaprogram tries to detect the @[unproved] tag, but this
    can fail if unprovedAttr isn't in scope. As a fallback, we match
    axiom node names against names found by scanning source files.
    Merges with any existing Lean-side detections.
    """
    if not unproved_names:
        return nodes

    updated = dict(nodes)
    matched = 0
    for name, node in nodes.items():
        if node.is_unproved or node.kind != "axiom":
            continue
        short = name.rsplit(".", 1)[-1] if "." in name else name
        if name in unproved_names or short in unproved_names:
            updated[name] = GraphNode(
                name=node.name,
                kind=node.kind,
                is_class=node.is_class,
                is_auto_generated=node.is_auto_generated,
                has_sorry=node.has_sorry,
                is_unproved=True,
                instance_count=node.instance_count,
                type_head=node.type_head,
                deps=node.deps,
                field_deps=node.field_deps,
                tags=node.tags,
            )
            matched += 1

    if matched:
        logger.info("Marked %d axioms as unproved (from source scan)", matched)

    return updated
