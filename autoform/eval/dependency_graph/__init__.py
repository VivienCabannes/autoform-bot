# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Dependency graph builder for Lean 4 projects.

Produces an annotated DAG where each node (declaration) carries
structural tags and attributes for eval quality assessment.

Usage:
    graph = await build_dependency_graph(repo_dir, module_prefix)
    cone = graph.support_cone("theorem_3_2")
    print(cone.alerts)       # ["depends_on_vacuous_definition"]
    print(cone.flagged_nodes) # ["definition_2_5"]
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .builder import build_raw_graph
from .cone import support_cone as _support_cone
from .tagger import apply_graph_tags
from .types import GraphNode, SupportCone


@dataclass
class DependencyGraph:
    """An annotated dependency graph for a Lean 4 project."""

    nodes: dict[str, GraphNode]

    @property
    def size(self) -> int:
        return len(self.nodes)

    def support_cone(self, target: str) -> SupportCone:
        """Extract the support cone for a target declaration."""
        return _support_cone(target, self.nodes)

    def get(self, name: str) -> GraphNode | None:
        return self.nodes.get(name)

    def save(self, path: Path) -> None:
        """Save the graph to a JSON file."""
        data = {name: asdict(node) for name, node in self.nodes.items()}
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path) -> DependencyGraph:
        """Load a graph from a JSON file."""
        raw = json.loads(path.read_text())
        nodes = {
            name: GraphNode(
                name=d["name"],
                kind=d["kind"],
                is_class=d.get("is_class", False),
                is_auto_generated=d.get("is_auto_generated", False),
                has_sorry=d.get("has_sorry", False),
                is_unproved=d.get("is_unproved", False),
                instance_count=d.get("instance_count", 0),
                type_head=d.get("type_head", ""),
                deps=tuple(d.get("deps", ())),
                field_deps=tuple(d.get("field_deps", ())),
                tags=tuple(d.get("tags", ())),
                all_deps=tuple(d.get("all_deps", ())),
                transitive_axioms=tuple(d.get("transitive_axioms", ())),
                cone_alerts=tuple(d.get("cone_alerts", ())),
            )
            for name, d in raw.items()
        }
        return cls(nodes=nodes)


def _compute_transitive_deps(nodes: dict[str, GraphNode]) -> dict[str, GraphNode]:
    """Compute transitive closure of deps for all nodes (memoized)."""
    cache: dict[str, set[str]] = {}

    def _all_deps(name: str, stack: set[str] | None = None) -> set[str]:
        if name in cache:
            return cache[name]
        if stack is None:
            stack = set()
        if name in stack:
            return set()  # cycle
        stack.add(name)
        node = nodes.get(name)
        if node is None:
            cache[name] = set()
            return set()
        result = set(node.deps)
        for dep in node.deps:
            result |= _all_deps(dep, stack)
        stack.discard(name)
        cache[name] = result
        return result

    updated: dict[str, GraphNode] = {}
    for name, node in nodes.items():
        all_deps = tuple(sorted(_all_deps(name)))

        # Compute transitive axioms: project axiom declarations + sorryAx
        trans_axioms: set[str] = set()
        if node.kind == "axiom":
            trans_axioms.add(name)
        if node.has_sorry:
            trans_axioms.add("sorryAx")

        # Compute cone alerts from transitive deps
        cone_tags: set[str] = set()
        has_sorry_dep = False
        has_opaque = False
        has_unproved_dep = False

        for dep_name in all_deps:
            dep = nodes.get(dep_name)
            if dep:
                if dep.kind == "axiom":
                    trans_axioms.add(dep_name)
                if dep.has_sorry:
                    trans_axioms.add("sorryAx")
                # Cone alert checks
                cone_tags.update(dep.tags)
                if dep.has_sorry and not dep.is_auto_generated:
                    has_sorry_dep = True
                if dep.kind == "opaque":
                    has_opaque = True
                if dep.is_unproved:
                    has_unproved_dep = True

        alerts: list[str] = []
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

        updated[name] = GraphNode(
            name=node.name,
            kind=node.kind,
            is_class=node.is_class,
            is_auto_generated=node.is_auto_generated,
            has_sorry=node.has_sorry,
            is_unproved=node.is_unproved,
            instance_count=node.instance_count,
            type_head=node.type_head,
            deps=node.deps,
            field_deps=node.field_deps,
            tags=node.tags,
            all_deps=all_deps,
            transitive_axioms=tuple(sorted(trans_axioms)),
            cone_alerts=tuple(alerts),
        )
    return updated


async def build_dependency_graph(
    repo_dir: Path,
    module_prefix: str,
    import_module: str | None = None,
    timeout: float = 3600,
) -> DependencyGraph:
    """Build an annotated dependency graph for a Lean 4 project.

    Args:
        repo_dir: Path to the Lean repository root.
        module_prefix: Module name prefix for project-local declarations.
        import_module: Module to import. Defaults to module_prefix.
        timeout: Max seconds for the Lean process.
    """
    import_mod = import_module or module_prefix

    # Step 1: Run Lean metaprogram, get raw nodes
    nodes = await build_raw_graph(repo_dir, module_prefix, import_mod, timeout)

    # Step 2: Apply graph-level tags
    nodes = apply_graph_tags(nodes)

    # Step 3: Compute transitive deps
    nodes = _compute_transitive_deps(nodes)

    return DependencyGraph(nodes=nodes)
