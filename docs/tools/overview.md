# Tools Reference

## Overview

Tools are exposed to agents as [MCP](https://modelcontextprotocol.io/) servers. Each tool server is a FastMCP instance that registers one or more tools. Agents declare which servers they need in their `config.yaml`; the framework resolves those keys into `MCPServerConfig` objects, connects to each server, discovers its tools, and routes tool calls through `MCPClientManager`.

## Available Tool Servers

| Config key | Server name | Transport | Purpose | Key tools |
|---|---|---|---|---|
| `ask_user` | ask-user | inprocess | Structured user interaction | `ask_user_question` |
| `bash` | bash | inprocess | Shell command execution with allowlist validation | `bash` |
| `bash_restricted` | bash_restricted | inprocess | Read-only shell (no interpreters, no file mutation) | `bash_restricted` |
| `cron` | cron | inprocess | Session-scoped scheduled prompts | `cron_create`, `cron_delete`, `cron_list` |
| `discovery` | discovery | inprocess | On-demand tool and skill documentation | `list_tools`, `check_tools`, `list_skills`, `check_skills` |
| `docs_lookup` | docs-lookup | inprocess | Search and fetch documentation pages | `search_docs`, `fetch_doc_page` |
| `email` | email | inprocess | IMAP/SMTP email operations | `read_inbox`, `get_email`, `search_emails`, `send_email`, `reply_to_email` |
| `filesystem` | filesystem | inprocess | Scoped file read/write/search operations | `read_text_file`, `write_file`, `edit_file`, `edit_lines`, `list_directory`, `directory_tree`, `search_files`, `file_grep`, `get_file_info`, `move_file`, `file_delete`, `create_directory`, `read_multiple_files`, `list_allowed_directories` |
| `gchat` | gchat | inprocess | Google Chat bridge operations | `gchat_list_spaces`, `gchat_list_messages`, `gchat_send_message`, `gchat_get_message` |
| `git` | git | inprocess | Git version control operations | `git_status`, `git_diff`, `git_log`, `git_show`, `git_branch`, `git_show_file`, `git_add`, `git_commit`, `git_checkout`, `git_restore`, `git_reset`, `git_rebase`, `git_rebase_continue`, `git_rebase_abort`, `git_rebase_skip`, `git_conflicts` |
| `glob_search` | glob | inprocess | File path glob search | `glob_search` |
| `grep` | grep | inprocess | Regex file-content search | `grep` |
| `latex` | latex-exec | inprocess | Compile LaTeX and inspect logs | `compile_latex`, `compile_latex_file`, `check_latex_engine` |
| `lean_repl` | lean-repl | streamable-http | Lean 4 REPL for type-checking code | `run_lean_code`, `get_repl_status` |
| `lsp` | lsp | streamable-http | Lean LSP (via lean-lsp-mcp binary) | Provided by [lean-lsp-mcp](https://github.com/leanprover/lean-lsp-mcp) |
| `lean_native_lsp` | lean-native-lsp | inprocess | Native Lean LSP with incremental checking | `lean_check_file`, `lean_proof_state` |
| `mathlib` | mathlib | inprocess | Search Mathlib source code | `mathlib_grep`, `mathlib_find_name`, `mathlib_read_file` |
| `notebook` | notebook | inprocess | Jupyter notebook read/edit operations | `read_notebook`, `edit_notebook_cell` |
| `pdf` | pdf | inprocess | PDF text extraction | `read_pdf` |
| `scratchpad` | scratchpad | inprocess | Scoped scratch files for agent notes | `scratchpad_read`, `scratchpad_write`, `scratchpad_list`, `scratchpad_delete` |
| `signal` | signal | inprocess | Signal bridge operations | `signal_list_groups`, `signal_send_message`, `signal_receive_messages` |
| `web_browse` | web-fetch, web-search | inprocess | Combined web fetch and web search | `web_fetch`, `web_search` |
| `web_fetch` | web-fetch | inprocess | Fetch and extract web pages | `web_fetch` |
| `web_search` | web-search | inprocess | Search the web | `web_search` |
| `worktree` | worktree | inprocess | Git worktree creation and management | `worktree_create`, `worktree_sync`, `worktree_has_commits`, `worktree_merge`, `worktree_cleanup`, `worktree_list` |
| `zulip` | zulip | inprocess | Zulip read operations | `zulip_list_streams`, `zulip_get_topics`, `zulip_get_messages`, `zulip_search_messages`, `zulip_get_direct_messages` |
| `trace_inspector`* | trace-inspector | inprocess | Query agent execution traces | `list_attempts`, `get_step_timeline`, `get_build_errors`, `get_review_feedback`, `list_agents`, `get_agent_stats`, `get_tool_stats`, `get_failed_tools`, `get_messages`, `get_tool_call` |

\* `trace_inspector` is instantiated directly with a `traces_dir` and `task_id`. The generic task dispatch, task tracker, sub-agent, and item tracker servers are also available as Python factories because they require runtime objects such as an `AgentPool`, `ItemTracker`, or `SubAgentManager`.

## Configuration

Agents declare tool servers in their `config.yaml` under the `servers` key:

```yaml
servers:
  - filesystem
  - bash
  - lean_repl
  - git
  - mathlib
```

Per-server configuration can be provided inline under a `server_config` key:

```yaml
server_config:
  bash:
    default_cwd: /path/to/workspace
  filesystem:
    allowed_dirs:
      - /path/to/workspace
    write_excluded_dirs:
      - /path/to/read-only-dir
```

## resolve_servers()

`tools.resolve_servers()` maps config key strings to `MCPServerConfig` objects.

```python
from tools import resolve_servers

configs = resolve_servers(
    ["filesystem", "bash", "lean_repl", "git"],
    workspace="/path/to/workspace",
)
```

**Resolution logic.** For each key in the list, `resolve_servers()` uses a `match` statement to:

1. Look up an explicit config override (passed as a keyword argument, e.g. `bash=BashConfig(...)`).
2. Fall back to parsing the key from `base_config` (the raw dict from agent YAML).
3. Use sensible defaults (typically scoped to `workspace`).

The result is a flat `list[MCPServerConfig]`, each carrying a `transport` method and either an in-process `mcp_instance` or a remote `url`. Unknown keys and failed resolutions log a warning and skip the server.

There is also `resolve_tool_scores()` which calls `resolve_servers()` purely for the side effect of populating the `ToolSpec` registry with autonomy scores (the servers themselves are discarded).

## Transport Methods

| Method | Description |
|---|---|
| `INPROCESS` | Direct Python calls to a FastMCP instance in the same process. No network. |
| `STREAMABLE_HTTP` | HTTP transport using MCP Streamable HTTP protocol. Used by `lean_repl` and `lsp`. |
| `SSE` | Legacy HTTP Server-Sent Events transport. |
| `STDIO` | Subprocess communicating over stdin/stdout. |
| `NPX` | Convenience wrapper around STDIO for npm packages. |

## Autonomy Levels

Every tool declares an autonomy level via `@ToolSpec.define(autonomy=...)`. This controls how much supervision the tool requires. Levels from lowest to highest:

| Level | Score | Meaning |
|---|---|---|
| `BARE` | 0 | No side effects (e.g. `list_allowed_directories`) |
| `READ` | 10 | Read-only access (e.g. `read_text_file`, `git_status`, `mathlib_grep`) |
| `WRITE` | 20 | Modifies files or state (e.g. `write_file`, `git_commit`) |
| `EXECUTE_RESTRICTED` | 25 | Restricted code execution (e.g. `bash_restricted`, `run_lean_code`) |
| `EXECUTE` | 30 | Unrestricted code execution (e.g. `bash`) |

An agent's overall autonomy is the maximum autonomy level among its allowed tools.

## Adding a New Tool

### 1. Create the server module

Place it under `tools/<category>/<name>/` with `core.py` (pure logic, no MCP dependencies) and `server.py` (FastMCP wrapper).

### 2. Define tools with the decorator pairing

Every `@server.tool` must be paired with `@ToolSpec.define(autonomy=...)`:

```python
from fastmcp.server import FastMCP
from core.tool import Autonomy, ToolSpec

server = FastMCP(name="my-tool")

@server.tool
@ToolSpec.define(autonomy=Autonomy.READ)
def my_tool(query: str) -> str:
    """Tool description shown to the agent.

    Args:
        query: What to look up.
    """
    return do_something(query)
```

### 3. Create a config dataclass and factory function

```python
from dataclasses import dataclass
from core.mcp import MCPServerConfig, TransportMethod

@dataclass(frozen=True)
class MyToolConfig:
    some_path: str

def my_tool_server(config: MyToolConfig) -> MCPServerConfig:
    mcp_instance = create_my_tool_server(config.some_path)
    return MCPServerConfig(
        server_key="my_tool",
        description="Brief description of the tool server",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
```

### 4. Register in resolve_servers()

Add a new `case` to the `match` statement in `tools/__init__.py`:

```python
case "my_tool":
    from tools.category.my_tool import my_tool_server
    mc = _get_config(my_tool, base_config, "my_tool", MyToolConfig) or MyToolConfig(some_path=workspace)
    configs.append(my_tool_server(mc))
```

Add the corresponding keyword parameter to the `resolve_servers()` signature and import the config class at the top of the file.
