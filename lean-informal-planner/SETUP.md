# Setup — Lean Informal Planner toolchain

The plugin's blueprint **view** (the interactive tiered dependency graph) is built
with [`leanblueprint`](https://github.com/PatrickMassot/leanblueprint) +
[`plasTeX`](https://github.com/plastex/plastex). This page covers installing that
toolchain and verifying it.

> **No LaTeX required.** The HTML/web blueprint build is **pure-Python plasTeX** —
> you do **not** need a TeX distribution to build or view the dependency graph.
> (A full PDF blueprint build would need LaTeX, but the plugin's view doesn't.)

## Quick start: Makefile

The exported blueprint project includes a `Makefile` with setup targets. From the
export directory:

```bash
# One-time setup: create venv and install all Python deps
make setup-venv

# Fetch Mathlib + deps (run as user, needs github access)
make setup-mathlib

# Build the HTML blueprint
make web

# Serve it locally
make serve
```

The `setup-venv` target creates a Python venv at `.venv/` and installs
`leanblueprint`, `plastexdepgraph`, `plastexshowmore`, `plasTeX`, `pygraphviz`,
and `fastmcp`. It attempts to detect graphviz headers for `pygraphviz` builds
(via `brew` on macOS or `graphviz-dev` on Debian/Ubuntu).

## Manual install: a dedicated venv (fallback)

If the Makefile's `setup-venv` fails (usually because graphviz headers aren't
found), use the manual path below.

### What you need

- **Python >= 3.10**
- **graphviz** (the `dot` binary on your `PATH`)
- Python packages: `plasTeX`, `plastexdepgraph`, `plastexshowmore`, `leanblueprint`, `pygraphviz`

### Install steps

```bash
# 1. graphviz (provides `dot` and the headers pygraphviz compiles against)
brew install graphviz            # macOS; on Debian/Ubuntu: sudo apt-get install graphviz graphviz-dev

# 2. a venv from a standard Python
brew install python@3.12         # if you don't already have a non-system python3
python3.12 -m venv ~/.venvs/lean-blueprint
source ~/.venvs/lean-blueprint/bin/activate

# 3. the toolchain
CFLAGS="-I$(brew --prefix graphviz)/include" \
LDFLAGS="-L$(brew --prefix graphviz)/lib" \
  pip install pygraphviz
pip install leanblueprint plastexdepgraph plastexshowmore plasTeX fastmcp
```

Then point the plugin at this interpreter:

```bash
export LEAN_PLANNER_PYTHON="$HOME/.venvs/lean-blueprint/bin/python"
```

(`LEAN_PLANNER_PYTHON` is honored by both `check_toolchain.sh` and the MCP server
launcher.)

On Debian/Ubuntu, install the graphviz **dev headers** instead of using `CFLAGS`/`LDFLAGS`:

```bash
sudo apt-get install graphviz graphviz-dev
pip install pygraphviz leanblueprint plastexdepgraph plastexshowmore plasTeX fastmcp
```

## Verify the install

Run the checker from anywhere:

```bash
bash lean-informal-planner/scripts/check_toolchain.sh
```

It prints a `PASS`/`FAIL` line for each requirement and, for every failure,
**the exact command to fix it**. It exits `0` only when everything passes.

## Lean / Mathlib setup

To build the Lean project and validate `\lean{}` declarations:

```bash
# Install elan (Lean version manager) if you don't have it
curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh

# From the project directory:
make setup-mathlib    # lake update + cache get + build
make checkdecls       # verify \lean{} names exist
```

## Viewing the built blueprint

The dependency graph uses a WASM module (`d3-graphviz`) that browsers only load
over HTTP. Always serve via `make serve` or `leanblueprint serve` rather than
opening `file://` URLs directly.
