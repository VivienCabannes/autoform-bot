"""Shared Autoform runtime helpers and directly executable CLI modules.

The command modules remain runnable by path from a plugin checkout.  Making the
directory a package also lets the review dashboard and installed wheel import
the shared queue, locking, backend, and accounting contracts reliably.
"""
