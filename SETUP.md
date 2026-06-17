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

The `setup-venv` target creates a Python venv at `.venv/` and installs a **pinned**
toolchain (`leanblueprint==0.0.20`, `plastexdepgraph==0.0.5`, `plastexshowmore==0.0.2`,
`plasTeX==3.1`, `pygraphviz`, `fastmcp`). It attempts to detect graphviz headers for
`pygraphviz` builds (via `brew` on macOS or `graphviz-dev` on Debian/Ubuntu). It also
runs `setup-gvlibs` (graphviz **runtime** lib curation, below) and applies the
`plastexdepgraph` hashability fix (below).

## Platform notes (two fixes `setup-venv` applies for you)

1. **Graphviz runtime libs (`setup-gvlibs`).** On "platform" Pythons (e.g. a vendored
   interpreter whose `libc` differs from the system one), `pygraphviz` can build but
   fail to load with `ImportError: libcdt.so.5: cannot open shared object file`, and
   putting all of `/usr/lib64` on `LD_LIBRARY_PATH` is wrong (it pulls in a
   conflicting system `libc`). `setup-gvlibs` copies **only** the graphviz shared
   libraries — preserving their soname symlinks (`libcdt.so.5 -> libcdt.so.5.0.0`,
   etc.) and including transitive deps like `libexpat`, `libltdl`, `libz` — into
   `.lean-deps/gvlibs/`. `make web`/`make serve` prepend that dir to
   `LD_LIBRARY_PATH`, so graphviz loads against the venv's own `libc`.

2. **`plastexdepgraph` hashability fix.** With plasTeX 3.1 (the only version
   `plastexdepgraph` accepts), the theorem environment classes created by
   leanblueprint's `\newtheorem` (e.g. `definition`) define `__eq__` without
   `__hash__`, so Python makes them unhashable and the dep-graph build dies at
   `graph.nodes = set(nodes)` with `TypeError: unhashable type: 'definition'`. Because
   no other plasTeX version is compatible, a version pin alone cannot avoid this;
   `setup-venv` therefore applies a tiny, idempotent post-install patch that restores
   `__hash__` on those classes. (Remove it once upstream `plastexdepgraph` fixes the
   `set(nodes)` usage.)

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
pip install "leanblueprint==0.0.20" "plastexdepgraph==0.0.5" "plastexshowmore==0.0.2" "plasTeX==3.1" fastmcp
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
pip install pygraphviz "leanblueprint==0.0.20" "plastexdepgraph==0.0.5" "plastexshowmore==0.0.2" "plasTeX==3.1" fastmcp
```

> The manual path still needs the **`plastexdepgraph` hashability fix** described under
> *Platform notes* above (otherwise the dep-graph build crashes with
> `unhashable type: 'definition'`). The Makefile's `make setup-venv` applies it
> automatically; if you install by hand, apply that one-line patch yourself, or just
> use the Makefile.

## Verify the install

Run the checker from anywhere:

```bash
bash scripts/check_toolchain.sh
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
