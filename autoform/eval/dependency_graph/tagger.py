# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Graph-level tagging (orphan_class, trivial_instance)."""

from __future__ import annotations

from .types import GraphNode

# Body-level tags that indicate a suspicious/trivial implementation
_SUSPICIOUS_BODY_TAGS = frozenset(
    {
        "vacuous_body",
        "ignores_params",
        "proof_by_exfalso",
        "proof_by_subsingleton",
        "returns_assumption",
        "trivial_constructor",
    }
)


def apply_graph_tags(nodes: dict[str, GraphNode]) -> dict[str, GraphNode]:
    """Apply graph-level tags that require cross-referencing across nodes.

    Tags applied:
        orphan_class: project class with zero real instances.
        trivial_instance: instance declaration that carries a suspicious body tag.
    """
    # Collect class names for type_head matching
    class_names = {n.name for n in nodes.values() if n.is_class}

    updated: dict[str, GraphNode] = {}

    for name, node in nodes.items():
        new_tags = list(node.tags)

        # orphan_class: class with no instances
        if node.is_class and node.instance_count == 0:
            new_tags.append("orphan_class")

        # trivial_instance: declaration whose type_head is a project class
        # and that has a suspicious body tag
        if (
            node.tags
            and _SUSPICIOUS_BODY_TAGS & set(node.tags)
            and node.type_head in class_names
            and not node.is_auto_generated
        ):
            new_tags.append("trivial_instance")

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
            tags=tuple(new_tags),
            all_deps=node.all_deps,
        )

    return updated
