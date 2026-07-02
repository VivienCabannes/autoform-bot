# REPL server — promoted

The Lean REPL server is no longer a stub: the real implementation lives at
[`servers/repl/`](../../../servers/repl/) (`core.py`, `pool.py`, `server.py`).
This directory used to hold a byte-identical copy; see the real module for the
current code and `tests/test_repl.py` for its unit tests.
