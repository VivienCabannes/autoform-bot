# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Support cone extraction and derived alerts."""

from __future__ import annotations

from collections import Counter

from .types import GraphNode, SupportCone


def support_cone(target: str, nodes: dict[str, GraphNode]) -> SupportCone:
    """Extract the transitive support cone for a target declaration.

    Returns a SupportCone with all nodes in the cone, derived alerts
    based on tags found in the cone, and a list of flagged node names.
    """
    cone_nodes: dict[str, GraphNode] = {}
    _collect_cone(target, nodes, cone_nodes)

    # Collect flagged nodes (any node with tags, excluding auto-generated)
    flagged = tuple(
        name for name, node in cone_nodes.items() if node.tags and name != target and not node.is_auto_generated
    )

    # Derive target-level alerts from cone tags
    alerts = _derive_alerts(cone_nodes, target)

    # Build human-readable summary
    summary = _build_summary(cone_nodes, target)

    return SupportCone(
        target=target,
        nodes=cone_nodes,
        alerts=alerts,
        flagged_nodes=flagged,
        summary=summary,
    )


def _collect_cone(
    name: str,
    all_nodes: dict[str, GraphNode],
    cone: dict[str, GraphNode],
) -> None:
    """DFS to collect all transitive deps into the cone."""
    if name in cone:
        return
    node = all_nodes.get(name)
    if node is None:
        return
    cone[name] = node
    for dep in node.deps:
        _collect_cone(dep, all_nodes, cone)


def _derive_alerts(cone: dict[str, GraphNode], target: str) -> tuple[str, ...]:
    """Derive target-level alerts from tags in the support cone."""
    alerts: list[str] = []

    # Collect all tags from non-target nodes in the cone
    cone_tags: set[str] = set()
    has_sorry_dep = False
    has_opaque = False
    has_unproved_dep = False

    for name, node in cone.items():
        if name != target:
            cone_tags.update(node.tags)
            if node.has_sorry and not node.is_auto_generated:
                has_sorry_dep = True
            if node.kind == "opaque":
                has_opaque = True
            if node.is_unproved:
                has_unproved_dep = True

    if "vacuous_body" in cone_tags:
        alerts.append("depends_on_vacuous_definition")
    if "orphan_class" in cone_tags:
        alerts.append("depends_on_orphan_class_field")
    if "trivial_instance" in cone_tags:
        alerts.append("depends_on_trivial_instance")
    if "proof_by_exfalso" in cone_tags:
        alerts.append("support_cone_contains_exfalso")
    if "proof_by_subsingleton" in cone_tags:
        alerts.append("support_cone_contains_subsingleton")
    if has_sorry_dep:
        alerts.append("depends_on_sorry_definition")
    if has_opaque:
        alerts.append("support_cone_contains_opaque")
    if has_unproved_dep:
        alerts.append("has_unproved_dependencies")

    return tuple(alerts)


def _build_summary(cone: dict[str, GraphNode], target: str) -> str:
    """Build a human-readable summary of the support cone."""
    deps = {name: node for name, node in cone.items() if name != target and not node.is_auto_generated}
    if not deps:
        return "No project dependencies."

    total = len(deps)

    # Count sorry
    sorry_count = sum(1 for n in deps.values() if n.has_sorry)

    # Count unproved
    unproved_count = sum(1 for n in deps.values() if n.is_unproved)

    # Count by kind
    kind_counts = Counter(n.kind for n in deps.values())

    # Count tags
    tag_counts: Counter[str] = Counter()
    for n in deps.values():
        for t in n.tags:
            tag_counts[t] += 1

    # Count clean (no sorry, no tags)
    clean = sum(1 for n in deps.values() if not n.has_sorry and not n.tags)

    # Build summary parts
    parts: list[str] = []
    parts.append(f"Support cone: {total} dependencies.")

    # Kind breakdown
    kind_str = ", ".join(f"{c} {k}s" for k, c in kind_counts.most_common())
    parts.append(f"By kind: {kind_str}.")

    # Proof status
    if sorry_count:
        parts.append(f"{sorry_count}/{total} dependencies use sorry ({sorry_count / total * 100:.0f}%).")
    if unproved_count:
        parts.append(f"{unproved_count}/{total} are intentionally unproved (book does not provide proof).")
    if clean:
        parts.append(f"{clean}/{total} are fully clean (no sorry, no flags).")

    # Flags
    if tag_counts:
        flag_parts = [f"{c} with {t}" for t, c in tag_counts.most_common()]
        parts.append(f"Flags: {', '.join(flag_parts)}.")

    # Specific concerns
    orphan_deps = [name for name, n in deps.items() if "orphan_class" in n.tags]
    if orphan_deps:
        parts.append(f"Depends on orphan classes (no instances): {', '.join(orphan_deps)}.")

    unproved_names = [name for name, n in deps.items() if n.is_unproved]
    if unproved_names:
        if len(unproved_names) <= 5:
            parts.append(f"Unproved declarations (book omits proof): {', '.join(unproved_names)}.")
        else:
            parts.append(f"Unproved declarations: {', '.join(unproved_names[:5])}, and {len(unproved_names) - 5} more.")

    sorry_names = [name for name, n in deps.items() if n.has_sorry and n.kind == "theorem"]
    if sorry_names:
        if len(sorry_names) <= 5:
            parts.append(f"Sorry'd theorems in chain: {', '.join(sorry_names)}.")
        else:
            parts.append(f"Sorry'd theorems in chain: {', '.join(sorry_names[:5])}, and {len(sorry_names) - 5} more.")

    stub_defs = [name for name, n in deps.items() if "ignores_params" in n.tags and n.kind == "def"]
    if stub_defs:
        if len(stub_defs) <= 5:
            parts.append(f"Stub definitions (ignore parameters): {', '.join(stub_defs)}.")
        else:
            parts.append(
                f"Stub definitions (ignore parameters): {', '.join(stub_defs[:5])}, and {len(stub_defs) - 5} more."
            )

    return " ".join(parts)
