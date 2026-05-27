# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Dependency graph query operations.

Wraps a DependencyGraph instance with query methods for use by
LLM judges investigating formalization quality.
"""

from __future__ import annotations

from collections import Counter

from autoform.eval.dependency_graph import DependencyGraph
from autoform.eval.dependency_graph.cone import support_cone
from autoform.eval.dependency_graph.types import GraphNode


class DepGraphOps:
    """Query operations over a dependency graph."""

    def __init__(self, graph: DependencyGraph) -> None:
        self._graph = graph

    def search_node(self, query: str, max_results: int = 20) -> str:
        """Search for declarations by name substring.

        Case-insensitive substring match against declaration names.
        Useful when the exact qualified name is unknown.
        """
        query_lower = query.lower()
        matches: list[GraphNode] = []
        for node in self._graph.nodes.values():
            if node.is_auto_generated:
                continue
            if query_lower in node.name.lower():
                matches.append(node)
                if len(matches) >= max_results:
                    break

        if not matches:
            return f"No declarations matching '{query}'."

        lines: list[str] = []
        lines.append(f"Found {len(matches)} declaration(s) matching '{query}':")
        lines.append("")
        for node in sorted(matches, key=lambda n: n.name):
            status_parts: list[str] = [node.kind]
            if node.has_sorry:
                status_parts.append("sorry")
            if node.is_unproved:
                status_parts.append("unproved")
            if node.tags:
                status_parts.extend(node.tags)
            lines.append(f"- {node.name} ({', '.join(status_parts)})")
        if len(matches) >= max_results:
            lines.append(f"(showing first {max_results} results)")
        return "\n".join(lines)

    def get_node(self, name: str) -> str:
        """Look up a declaration by name.

        Returns kind, tags, sorry status, direct dependencies,
        and other attributes.
        """
        node = self._graph.get(name)
        if node is None:
            return f"Declaration '{name}' not found in the graph."
        return _format_node(node)

    def get_dependency_health(self, name: str) -> str:
        """Analyze the health of a declaration's entire dependency chain.

        Returns alerts, flagged nodes, and a summary of the cone's health.
        """
        node = self._graph.get(name)
        if node is None:
            return f"Declaration '{name}' not found in the graph."
        cone = support_cone(name, self._graph.nodes)

        parts: list[str] = []
        parts.append(f"Target: {name}")
        parts.append(f"Cone size: {len(cone.nodes) - 1} dependencies")

        # Use precomputed cone_alerts from the graph node
        alerts = node.cone_alerts
        if alerts:
            parts.append(f"Alerts: {', '.join(alerts)}")
        else:
            parts.append("Alerts: none")

        if cone.flagged_nodes:
            parts.append(f"Flagged nodes: {', '.join(cone.flagged_nodes)}")

        parts.append("")
        parts.append(cone.summary)
        return "\n".join(parts)

    def list_dependencies(self, name: str, transitive: bool = False) -> str:
        """List dependencies of a declaration with their status.

        Each dependency is shown with its kind, sorry status, and tags.
        """
        node = self._graph.get(name)
        if node is None:
            return f"Declaration '{name}' not found in the graph."

        deps = node.all_deps if transitive else node.deps
        if not deps:
            return f"'{name}' has no {'transitive ' if transitive else ''}project dependencies."

        lines: list[str] = []
        header = "transitive" if transitive else "direct"
        lines.append(f"{len(deps)} {header} dependencies of {name}:")
        lines.append("")

        for dep_name in sorted(deps):
            dep_node = self._graph.get(dep_name)
            if dep_node is None:
                lines.append(f"- {dep_name} (not in graph)")
                continue
            if dep_node.is_auto_generated:
                continue
            status_parts: list[str] = [dep_node.kind]
            if dep_node.has_sorry:
                status_parts.append("sorry")
            if dep_node.is_unproved:
                status_parts.append("unproved")
            if dep_node.tags:
                status_parts.extend(dep_node.tags)
            status = ", ".join(status_parts)
            lines.append(f"- {dep_name} ({status})")

        return "\n".join(lines)

    def list_suspicious_dependencies(self, name: str) -> str:
        """List all problematic nodes in a declaration's dependency chain.

        Shows dependencies with structural issues like vacuous bodies,
        orphan classes, degenerate proofs, or ignored parameters.
        """
        node = self._graph.get(name)
        if node is None:
            return f"Declaration '{name}' not found in the graph."

        cone = support_cone(name, self._graph.nodes)
        if not cone.flagged_nodes:
            return f"No flagged nodes in the support cone of '{name}'."

        lines: list[str] = []
        lines.append(f"Flagged nodes in cone of {name}:")
        lines.append("")
        for flagged_name in cone.flagged_nodes:
            flagged_node = self._graph.get(flagged_name)
            if flagged_node:
                lines.append(f"- {flagged_name} ({flagged_node.kind})")
                lines.append(f"  Tags: {', '.join(flagged_node.tags)}")
                if flagged_node.has_sorry:
                    lines.append("  Has sorry: yes")
                if flagged_node.is_unproved:
                    lines.append("  Unproved: yes (book does not provide proof)")
        return "\n".join(lines)

    def trace_sorry_dependencies(self, name: str) -> str:
        """Trace sorry usage through a declaration's dependency chain.

        Shows which dependencies use sorry and whether they are
        direct or transitive, helping distinguish shallow vs deep sorry.
        Unproved declarations (intentional axioms) are shown separately.
        """
        node = self._graph.get(name)
        if node is None:
            return f"Declaration '{name}' not found in the graph."

        direct_sorry: list[str] = []
        transitive_sorry: list[str] = []
        unproved: list[str] = []
        direct_deps = set(node.deps)

        cone = support_cone(name, self._graph.nodes)
        for dep_name, dep_node in cone.nodes.items():
            if dep_name == name or dep_node.is_auto_generated:
                continue
            if dep_node.is_unproved:
                unproved.append(dep_name)
            elif dep_node.has_sorry:
                if dep_name in direct_deps:
                    direct_sorry.append(dep_name)
                else:
                    transitive_sorry.append(dep_name)

        if not direct_sorry and not transitive_sorry and not unproved:
            self_sorry = "yes" if node.has_sorry else "no"
            return f"No sorry or unproved in the dependency chain of '{name}'. Target itself has sorry: {self_sorry}."

        lines: list[str] = []
        lines.append(f"Sorry/unproved chain for {name} (target has sorry: {node.has_sorry}):")
        lines.append("")

        if direct_sorry:
            lines.append(f"Direct dependencies with sorry ({len(direct_sorry)}):")
            for s in sorted(direct_sorry):
                dep = self._graph.get(s)
                kind = dep.kind if dep else "?"
                lines.append(f"  - {s} ({kind})")

        if transitive_sorry:
            lines.append(f"Transitive dependencies with sorry ({len(transitive_sorry)}):")
            for s in sorted(transitive_sorry):
                dep = self._graph.get(s)
                kind = dep.kind if dep else "?"
                lines.append(f"  - {s} ({kind})")

        if unproved:
            lines.append(f"Intentionally unproved — book does not provide proof ({len(unproved)}):")
            for u in sorted(unproved):
                lines.append(f"  - {u} (axiom)")

        total = len(cone.nodes) - 1
        sorry_count = len(direct_sorry) + len(transitive_sorry)
        if total > 0 and sorry_count > 0:
            lines.append("")
            lines.append(f"Sorry coverage: {sorry_count}/{total} dependencies ({sorry_count / total * 100:.0f}%)")
        return "\n".join(lines)

    def find_dependents(self, name: str) -> str:
        """Find all declarations that directly depend on a given declaration.

        Useful for assessing the impact of a problematic declaration
        and detecting dead code (declarations nothing depends on).
        """
        if name not in self._graph.nodes:
            return f"Declaration '{name}' not found in the graph."

        dependents: list[str] = []
        for node_name, node in self._graph.nodes.items():
            if node.is_auto_generated:
                continue
            if name in node.deps:
                dependents.append(node_name)

        if not dependents:
            return f"No declarations depend on '{name}' (potential dead code)."

        lines: list[str] = []
        lines.append(f"{len(dependents)} declarations directly depend on {name}:")
        lines.append("")
        for dep_name in sorted(dependents):
            dep = self._graph.get(dep_name)
            if dep:
                lines.append(f"- {dep_name} ({dep.kind})")
        return "\n".join(lines)

    def overview(self) -> str:
        """Get a high-level overview of the project graph.

        Shows total declarations, breakdown by kind, sorry/tag counts,
        and top-level health metrics.
        """
        nodes = [n for n in self._graph.nodes.values() if not n.is_auto_generated]
        total = len(nodes)

        kind_counts = Counter(n.kind for n in nodes)
        sorry_count = sum(1 for n in nodes if n.has_sorry)
        unproved_count = sum(1 for n in nodes if n.is_unproved)
        tagged_count = sum(1 for n in nodes if n.tags)

        tag_counts: Counter[str] = Counter()
        for n in nodes:
            for t in n.tags:
                tag_counts[t] += 1

        # Dead code: nodes with no reverse deps (excluding auto-generated)
        all_deps: set[str] = set()
        for n in nodes:
            all_deps.update(n.deps)
        no_dependents = [n.name for n in nodes if n.name not in all_deps]

        lines: list[str] = []
        lines.append(f"Project graph: {total} declarations")
        lines.append("")

        lines.append("By kind:")
        for kind, count in kind_counts.most_common():
            lines.append(f"  {kind}: {count}")

        lines.append("")
        lines.append(f"Sorry: {sorry_count}/{total} ({sorry_count / total * 100:.0f}%)")
        if unproved_count:
            lines.append(f"Unproved (book omits proof): {unproved_count}/{total}")
        lines.append(f"Flagged: {tagged_count}/{total} ({tagged_count / total * 100:.0f}%)")

        if tag_counts:
            lines.append("")
            lines.append("Tag breakdown:")
            for tag, count in tag_counts.most_common():
                lines.append(f"  {tag}: {count}")

        if no_dependents:
            lines.append("")
            lines.append(f"Potential dead code (no dependents): {len(no_dependents)}")

        return "\n".join(lines)


def _format_node(node: GraphNode) -> str:
    """Format a GraphNode as human-readable text."""
    lines: list[str] = []
    lines.append(f"Name: {node.name}")
    lines.append(f"Kind: {node.kind}")
    lines.append(f"Has sorry: {node.has_sorry}")
    if node.is_unproved:
        lines.append("Unproved: yes (book does not provide proof)")

    if node.is_class:
        lines.append(f"Is class: yes (instances: {node.instance_count})")
    if node.type_head:
        lines.append(f"Type head: {node.type_head}")
    if node.tags:
        lines.append(f"Tags: {', '.join(node.tags)}")
    if node.deps:
        lines.append(f"Direct deps ({len(node.deps)}): {', '.join(sorted(node.deps))}")
    if node.field_deps:
        lines.append(f"Field deps: {', '.join(sorted(node.field_deps))}")
    if node.all_deps:
        lines.append(f"Transitive deps: {len(node.all_deps)}")

    return "\n".join(lines)
