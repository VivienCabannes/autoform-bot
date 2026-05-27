# Lean REPL Setup

The Lean REPL is declared as a Lake dependency in the workspace project at `template/`. Building the workspace fetches and compiles the REPL automatically.

## Setup

```bash
make workspace
```

Or manually:

```bash
cd template
lake exe cache get
lake build REPL
cd ..
```

The binary is built at `submodules/repl/.lake/build/bin/repl`.

## How It Works

The REPL is a standalone binary that reads JSON commands from stdin and returns results. It runs via `lake env` from the workspace directory — `lake env` sets up `LEAN_PATH` so the binary can resolve imports like `import Mathlib`.

This is the same pattern used by [kimina-lean-server](https://github.com/project-numina/kimina-lean-server).

The `LeanRepl` class manages the subprocess. Configure it with:

```python
from tools.execution.lean.repl import LeanRepl, LeanReplConfig

repl_bin = "../submodules/repl/.lake/build/bin/repl"
config = LeanReplConfig(
    cwd="template",
    repl_command=["lake", "env", repl_bin],
)

repl = LeanRepl(config)
repl.start()
result = repl.run("#check Nat")
```

## Toolchain Version

The workspace's `lean-toolchain` must match Mathlib's. Since the workspace references Mathlib via a relative path, Lake ensures toolchain consistency at build time.

## Pooled REPL Server

For multi-agent setups, The framework runs the REPL as a pooled HTTP server:

```python
from tools.execution.lean.repl import LeanReplPoolConfig, run_lean_repl_server

repl_bin = "../submodules/repl/.lake/build/bin/repl"
config = LeanReplPoolConfig(
    cwd="template",
    repl_command=["lake", "env", repl_bin],
    num_repls=4,       # number of parallel REPL instances
    port=8990,
)
run_lean_repl_server(config)  # blocks, serving on http://127.0.0.1:8990/mcp
```

Agents connect via `lean_repl_server()` which returns an `MCPServerConfig` pointing to the server URL.
