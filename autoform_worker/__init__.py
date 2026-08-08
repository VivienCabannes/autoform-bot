"""Autoform worker CLI — the distributed-coordination layer.

Multiple machines advance a Markdown-authored formalization roadmap and its
Lean sources with minimal human coordination. Modeled on TauCetiWorker: CAS
branch pushes for safety, fail-closed Git-ref lease claims for throughput, and
scoreboard comments for cross-machine review state.

See ``docs/worker-cli.md`` for the design contract.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("autoform")
except PackageNotFoundError:  # source checkout without an installed distribution
    __version__ = "0+unknown"
