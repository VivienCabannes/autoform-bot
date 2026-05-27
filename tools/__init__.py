# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP tool servers (generic, reusable)."""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from core.mcp import MCPServerConfig
from tools.execution.bash.server import BashConfig, BashRestrictedConfig
from tools.execution.lean.lsp.server import LspConfig
from tools.execution.lean.native_lsp.server import LeanNativeLspConfig
from tools.execution.lean.repl.server import ReplConfig
from tools.files.filesystem.server import FilesystemConfig
from tools.search.mathlib.server import MathlibConfig
from tools.vcs.git.server import GitConfig

logger = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────────

_T = TypeVar("_T")


def _get_config(
    override: _T | None,
    base: dict[str, dict[str, Any]] | None,
    key: str,
    cls: type[_T],
) -> _T | None:
    """Return *override* if provided, else parse *key* from *base* dict.

    Note: configs with non-serializable fields (e.g. ``BashConfig.command_approval_handler``)
    cannot be loaded from YAML ``base_config`` and must be provided as explicit overrides.
    """
    if override is not None:
        return override
    if base and key in base:
        return cls(**base[key])
    return None


# ── resolve_servers ──────────────────────────────────────────────────


def resolve_servers(
    keys: list[str],
    *,
    workspace: str = ".",
    base_config: dict[str, dict[str, Any]] | None = None,
    bash: BashConfig | None = None,
    bash_restricted: BashRestrictedConfig | None = None,
    filesystem: FilesystemConfig | None = None,
    lean_repl: ReplConfig | None = None,
    lsp: LspConfig | None = None,
    lean_native_lsp: LeanNativeLspConfig | None = None,
    mathlib: MathlibConfig | None = None,
    git: GitConfig | None = None,
) -> list[MCPServerConfig]:
    """Map server key strings (from config.yaml) to MCPServerConfig objects.

    Args:
        keys: Server keys, e.g. ["filesystem", "bash", "lsp", "mathlib", "git"].
        workspace: Working directory for filesystem server and default for
            repo-path servers.
        base_config: Raw dict from agent definition YAML (parsed per-key on demand).
        bash: Override BashConfig (wins over base_config).
        bash_restricted: Override BashRestrictedConfig.
        filesystem: Override FilesystemConfig.
        lean_repl: Override ReplConfig.
        lsp: Override LspConfig.
        lean_native_lsp: Override LeanNativeLspConfig.
        mathlib: Override MathlibConfig.
        git: Override GitConfig.

    Returns:
        Flat list of MCPServerConfig objects.

    Raises:
        ValueError: If an unknown server key is encountered.
    """
    configs: list[MCPServerConfig] = []
    for key in keys:
        try:
            match key:
                # execution/
                case "bash":
                    from tools.execution.bash import bash_server

                    bc = _get_config(bash, base_config, "bash", BashConfig) or BashConfig(default_cwd=workspace)
                    configs.append(bash_server(bc))
                case "bash_restricted":
                    from tools.execution.bash import bash_restricted_server

                    brc = _get_config(
                        bash_restricted, base_config, "bash_restricted", BashRestrictedConfig
                    ) or BashRestrictedConfig(default_cwd=workspace)
                    configs.append(bash_restricted_server(brc))
                case "lean_repl":
                    from tools.execution.lean.repl import repl_server_config

                    rc = _get_config(lean_repl, base_config, "lean_repl", ReplConfig) or ReplConfig()
                    configs.append(repl_server_config(rc))
                case "lsp":
                    from tools.execution.lean.lsp import lsp_server_config

                    lc = _get_config(lsp, base_config, "lsp", LspConfig) or LspConfig(project_path=workspace)
                    configs.append(lsp_server_config(lc))
                case "lean_native_lsp":
                    from tools.execution.lean.native_lsp import lean_native_lsp_server

                    lnlc = _get_config(
                        lean_native_lsp, base_config, "lean_native_lsp", LeanNativeLspConfig
                    ) or LeanNativeLspConfig(workspace=workspace)
                    configs.append(lean_native_lsp_server(lnlc))
                # files/
                case "filesystem":
                    from tools.files.filesystem import filesystem_server

                    fc = _get_config(filesystem, base_config, "filesystem", FilesystemConfig) or FilesystemConfig(
                        allowed_dirs=(workspace,)
                    )
                    configs.append(filesystem_server(fc))
                # search/
                case "mathlib":
                    from tools.search.mathlib import mathlib_server

                    mc = _get_config(mathlib, base_config, "mathlib", MathlibConfig) or MathlibConfig(
                        repo_root=workspace
                    )
                    configs.append(mathlib_server(mc))
                # vcs/
                case "git":
                    from tools.vcs.git import git_server

                    gc = _get_config(git, base_config, "git", GitConfig) or GitConfig(repo_root=workspace)
                    configs.append(git_server(gc))
                case _:
                    raise ValueError(f"Unknown tool server key: {key!r}")
        except Exception:
            logger.warning("Failed to resolve tool server %r, skipping", key, exc_info=True)
    return configs


def resolve_tool_scores(keys: list[str], **kwargs: Any) -> None:
    """Resolve servers to populate the ToolSpec registry with tool autonomy scores.

    Accepts the same keyword arguments as ``resolve_servers()`` (workspace,
    base_config, and per-server config overrides).

    This is a side-effect-only function — it creates servers temporarily
    so that @ToolSpec.define() decorators execute and register scores.
    The servers are discarded after.
    """
    resolve_servers(keys, **kwargs)
