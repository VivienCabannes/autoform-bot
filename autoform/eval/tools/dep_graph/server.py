# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Dependency graph MCP server — tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from autoform.eval.dependency_graph import DependencyGraph

from .core import DepGraphOps


def create_dep_graph_server(graph: DependencyGraph) -> FastMCP:
    """Create an MCP server exposing dependency graph query tools."""
    ops = DepGraphOps(graph)
    server = FastMCP(name="dep_graph")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def search_node(query: str, max_results: int = 20) -> str:
        """Search for declarations by name substring.

        Case-insensitive substring match. Use this when the exact
        qualified name is unknown or to explore what declarations
        exist for a given concept.

        Args:
            query: Substring to search for in declaration names.
            max_results: Maximum number of results to return.
        """
        return ops.search_node(query, max_results=max_results)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_node(name: str) -> str:
        """Look up a declaration in the dependency graph.

        Returns the declaration's kind, tags, sorry status, direct
        dependencies, and other structural attributes.

        Args:
            name: Fully qualified Lean declaration name.
        """
        return ops.get_node(name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_dependency_health(name: str) -> str:
        """Analyze the health of a declaration's entire dependency chain.

        Returns alerts about structural issues (sorry, vacuous definitions,
        orphan classes), lists problematic nodes, and summarizes overall
        health. Use this to assess whether a formalization is built on
        solid foundations.

        Args:
            name: Fully qualified Lean declaration name.
        """
        return ops.get_dependency_health(name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_dependencies(name: str, transitive: bool = False) -> str:
        """List dependencies of a declaration with their status.

        Each dependency is shown with its kind, sorry status, and
        structural tags. Use transitive=true to see the full chain.

        Args:
            name: Fully qualified Lean declaration name.
            transitive: If true, show all transitive deps. Otherwise direct only.
        """
        return ops.list_dependencies(name, transitive=transitive)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_suspicious_dependencies(name: str) -> str:
        """List all problematic dependencies in a declaration's chain.

        Shows dependencies with structural issues like vacuous bodies,
        orphan classes, degenerate proofs, or ignored parameters.

        Args:
            name: Fully qualified Lean declaration name.
        """
        return ops.list_suspicious_dependencies(name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def trace_sorry_dependencies(name: str) -> str:
        """Trace sorry usage through a declaration's dependency chain.

        Shows which dependencies use sorry, distinguishing between
        direct dependencies (immediate) and transitive ones (deeper
        in the chain). Also shows intentionally unproved declarations
        separately.

        Args:
            name: Fully qualified Lean declaration name.
        """
        return ops.trace_sorry_dependencies(name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def find_dependents(name: str) -> str:
        """Find all declarations that directly depend on a given declaration.

        Useful for assessing the impact of a problematic declaration
        and detecting dead code (declarations nothing depends on).

        Args:
            name: Fully qualified Lean declaration name.
        """
        return ops.find_dependents(name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def overview() -> str:
        """Get a high-level overview of the project dependency graph.

        Shows total declarations, breakdown by kind, sorry and tag
        counts, and identifies potential dead code.
        """
        return ops.overview()

    return server


def dep_graph_server(graph: DependencyGraph) -> MCPServerConfig:
    """Create a dependency graph MCP server config."""
    return MCPServerConfig(
        server_key="dep_graph",
        description="Query the project dependency graph for structural analysis",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_dep_graph_server(graph),
    )
