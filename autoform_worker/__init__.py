"""Autoform worker CLI — the distributed-coordination layer.

Multiple machines advance one shared formalization roadmap (a GitHub repo holding
``graph.json`` + Lean sources) with minimal human coordination. Modeled on
TauCetiWorker: CAS branch pushes for safety, git-ref lease claims for
throughput, scoreboard comments for cross-machine review state.

See ``docs/worker-cli.md`` for the design contract.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("autoform")
except PackageNotFoundError:  # source checkout without an installed distribution
    __version__ = "0+unknown"
