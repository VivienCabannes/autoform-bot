# Setup — Lean Informal Planner toolchain

The plugin's blueprint **view** (the interactive tiered dependency graph) is built
with [`leanblueprint`](https://github.com/PatrickMassot/leanblueprint) +
[`plasTeX`](https://github.com/plastex/plastex). This page covers installing that
toolchain and verifying it.

> **No LaTeX required.** The HTML/web blueprint build is **pure-Python plasTeX** —
> you do **not** need a TeX distribution to build or view the dependency graph.
> (A full PDF blueprint build would need LaTeX, but the plugin's view doesn't.)

## What you need

- **Python ≥ 3.10**
- **graphviz** (the `dot` binary on your `PATH`)
- Python packages: `plasTeX`, `plastexdepgraph`, `plastexshowmore`, `leanblueprint`, `pygraphviz`

## Recommended install: a dedicated venv (robust)

The most robust path is a **dedicated virtualenv created from a standard Python**
(e.g. Homebrew's `python@3.12`). A venv from a standard Python builds
`pygraphviz` cleanly **without** any special linker flags.

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
pip install leanblueprint plastexdepgraph plastexshowmore plasTeX
```

Then point the plugin at this interpreter:

```bash
export LEAN_PLANNER_PYTHON="$HOME/.venvs/lean-blueprint/bin/python"
```

(`LEAN_PLANNER_PYTHON` is honored by both `check_toolchain.sh` and the MCP server
launcher. The check script treats a pinned interpreter as authoritative.)

## Alternative: install into an existing Python

You can install into any Python ≥ 3.10 (system or `--user`). On **nonstandard
system Pythons** (notably the Meta build) the `pygraphviz` C extension needs an
extra linker flag, `-undefined dynamic_lookup`:

```bash
brew install graphviz
CFLAGS="-I$(brew --prefix graphviz)/include" \
LDFLAGS="-L$(brew --prefix graphviz)/lib -undefined dynamic_lookup" \
  pip install pygraphviz          # the flag is only needed on nonstandard system Python
pip install leanblueprint plastexdepgraph plastexshowmore plasTeX
```

The `-undefined dynamic_lookup` flag is the documented **fallback**. A venv from a
standard Python (the recommended path above) builds `pygraphviz` without it, which
is why that path is preferred.

On Debian/Ubuntu, install the graphviz **dev headers** instead of using `CFLAGS`/`LDFLAGS`:

```bash
sudo apt-get install graphviz graphviz-dev
pip install pygraphviz leanblueprint plastexdepgraph plastexshowmore plasTeX
```

## Verify the install

Run the checker from anywhere:

```bash
bash lean-informal-planner/scripts/check_toolchain.sh
# or, if executable:
./lean-informal-planner/scripts/check_toolchain.sh
```

It prints a `PASS`/`FAIL` line for each requirement (Python ≥ 3.10, `dot`, and
each Python import) and, for every failure, **the exact command to fix it**. It
exits `0` only when everything passes.

To check a specific interpreter, pin it first:

```bash
LEAN_PLANNER_PYTHON=/path/to/python bash lean-informal-planner/scripts/check_toolchain.sh
```

## Viewing the built blueprint — serve over HTTP, not `file://`

The dependency graph is laid out client-side by a **WASM** module
(`d3-graphviz`). Browser WASM workers will **not** load from a `file://` URL, so
you must serve the built view over a **local HTTP server**:

```bash
leanblueprint serve          # serves the built web blueprint over http://localhost:...
```

Opening the generated `index.html` / `dep_graph.html` directly with `file://`
will leave the graph blank — always use a local HTTP server.

## Troubleshooting

- **`dot: command not found`** — install graphviz (`brew install graphviz` / `apt-get install graphviz`).
- **`import pygraphviz` fails to build** — ensure graphviz (and its dev headers
  on Linux) are installed; on a nonstandard system Python add
  `-undefined dynamic_lookup` to `LDFLAGS` (see above), or switch to the venv path.
- **Wrong Python picked up** — set `LEAN_PLANNER_PYTHON` to the interpreter that
  has the packages, then re-run `check_toolchain.sh`.
- **Graph is blank in the browser** — you opened it via `file://`; serve it over
  HTTP instead.
