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
from tools.discovery.server import DiscoveryConfig
from tools.execution.bash.server import BashConfig, BashRestrictedConfig
from tools.execution.latex.core import LatexConfig
from tools.execution.lean.lsp.server import LspConfig
from tools.execution.lean.native_lsp.server import LeanNativeLspConfig
from tools.execution.lean.repl.server import ReplConfig
from tools.files.filesystem.server import FilesystemConfig
from tools.search.mathlib.server import MathlibConfig
from tools.vcs.git.server import GitConfig
from tools.workspace.scratchpad.server import ScratchpadConfig

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
    latex: LatexConfig | None = None,
    lean_repl: ReplConfig | None = None,
    lsp: LspConfig | None = None,
    lean_native_lsp: LeanNativeLspConfig | None = None,
    mathlib: MathlibConfig | None = None,
    git: GitConfig | None = None,
    scratchpad: ScratchpadConfig | None = None,
    discovery: DiscoveryConfig | None = None,
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
        latex: Override LatexConfig.
        lean_repl: Override ReplConfig.
        lsp: Override LspConfig.
        lean_native_lsp: Override LeanNativeLspConfig.
        mathlib: Override MathlibConfig.
        git: Override GitConfig.
        scratchpad: Override ScratchpadConfig.
        discovery: Override DiscoveryConfig (pass a shared ToolRegistry via this).

    Returns:
        Flat list of MCPServerConfig objects.

    Raises:
        ValueError: If an unknown server key is encountered.
    """
    configs: list[MCPServerConfig] = []
    for key in keys:
        try:
            match key:
                # communication/
                case "ask_user":
                    from tools.communication.ask_user import ask_user_server

                    configs.append(ask_user_server())
                case "email":
                    from tools.communication.email import email_server

                    configs.append(email_server())
                case "gchat":
                    from tools.communication.gchat import gchat_server

                    configs.append(gchat_server())
                case "signal":
                    from tools.communication.signal import signal_server

                    configs.append(signal_server())
                case "zulip":
                    from tools.communication.zulip import zulip_server

                    configs.append(zulip_server())
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
                case "latex":
                    from tools.execution.latex.server import latex_exec_server

                    lxc = _get_config(latex, base_config, "latex", LatexConfig) or LatexConfig(cwd=workspace)
                    configs.append(latex_exec_server(config=lxc))
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
                case "pdf":
                    from tools.files.pdf import pdf_server

                    configs.append(pdf_server(allowed_dirs=[workspace]))
                case "filesystem":
                    from tools.files.filesystem import filesystem_server

                    fc = _get_config(filesystem, base_config, "filesystem", FilesystemConfig) or FilesystemConfig(
                        allowed_dirs=(workspace,)
                    )
                    configs.append(filesystem_server(fc))
                case "notebook":
                    from tools.files.notebook import notebook_server

                    configs.append(notebook_server(allowed_dirs=[workspace]))
                # search/
                case "mathlib":
                    from tools.search.mathlib import mathlib_server

                    mc = _get_config(mathlib, base_config, "mathlib", MathlibConfig) or MathlibConfig(
                        repo_root=workspace
                    )
                    configs.append(mathlib_server(mc))
                case "grep":
                    from tools.search.grep import grep_server

                    configs.append(grep_server(allowed_dirs=[workspace]))
                case "glob_search":
                    from tools.search.glob_search import glob_search_server

                    configs.append(glob_search_server(allowed_dirs=[workspace]))
                # vcs/
                case "git":
                    from tools.vcs.git import git_server

                    gc = _get_config(git, base_config, "git", GitConfig) or GitConfig(repo_root=workspace)
                    configs.append(git_server(gc))
                case "worktree":
                    from tools.vcs.worktree import worktree_server

                    configs.append(worktree_server(repo_root=workspace))
                # web/
                case "docs_lookup":
                    from tools.web.docs_lookup import docs_lookup_server

                    configs.append(docs_lookup_server(**(base_config or {}).get("docs_lookup", {})))
                case "web_browse":
                    from tools.web.web_browse import web_browse_server

                    configs.extend(web_browse_server())
                case "web_fetch":
                    from tools.web.web_fetch import web_fetch_server

                    configs.append(web_fetch_server(**(base_config or {}).get("web_fetch", {})))
                case "web_search":
                    from tools.web.web_search import web_search_server

                    configs.append(web_search_server(**(base_config or {}).get("web_search", {})))
                # workspace/
                case "cron":
                    from tools.workspace.cron import cron_server

                    configs.append(cron_server())
                case "scratchpad":
                    from tools.workspace.scratchpad import scratchpad_server

                    sc = _get_config(scratchpad, base_config, "scratchpad", ScratchpadConfig) or ScratchpadConfig()
                    configs.append(scratchpad_server(sc))
                # discovery/
                case "discovery":
                    from tools.discovery import discovery_server as _discovery_server

                    dc = discovery or DiscoveryConfig()
                    configs.append(_discovery_server(dc))
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
