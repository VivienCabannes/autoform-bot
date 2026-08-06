"""Shared constants for the worker CLI — stages, budgets, timing, markers.

Values follow TauCetiWorker's field-tested numbers where the semantics match
(claim TTL/heartbeat, round timeout, backoff curve, attempt budgets).
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Work-unit cascade. One round executes at most one unit; the first stage with
# an actionable candidate wins. Tending existing PRs always precedes generating
# new work (`prove` last — it is the expensive, work-creating stage).
# ---------------------------------------------------------------------------
#: ``agents`` drains every registry-discovered role kind (planner, mathcheck,
#: graphreview, contentreview, counterexample, priorart, holistic, escalation,
#: plus anything a project adds as a role file). It sits before ``prove`` so the
#: roadmap is planned, checked, and refuted-against before compute is spent
#: proving it — and after the PR-tending stages so open work always drains first.
STAGES = ("rebase", "fix-ci", "fix", "review", "merge", "progress", "agents", "prove")

MAX_AGENT_ATTEMPTS = 3

# ---------------------------------------------------------------------------
# Auto-merge gate. Humans are in the loop through the dashboards (the static
# roadmap site + the local review dashboard), not through merge buttons: a PR
# merges automatically when CI is green, a TRUSTED scoreboard says `clean` at
# the exact head, it touches only roadmap-content paths, no hold label is set,
# and no human verdict blocks the node. A human `rejected`/`flagged` verdict in
# the sidecar (recorded via the local dashboard) always blocks the gate.
# ---------------------------------------------------------------------------
HOLD_LABELS = frozenset({"hold", "human", "wip", "keep", "do-not-close"})
MAX_MERGE_ATTEMPTS = 3

_MERGE_PATH_ALLOW = (
    r"^[^/]+\.lean$",            # top-level lean (rare)
    r"^[A-Za-z0-9_./-]+\.lean$",  # library lean files
    r"^kernel/",                  # kernel evidence
    r"^blueprint/",               # Markdown roadmap and mathematical sources
    r"^informal_content/",        # legacy node prose during migration
    r"^review_status\.json$",     # sidecar folds
)
_MERGE_PATH_DENY = (
    r"^\.github/", r"^\.claude", r"^\.codex", r"^\.autoform/",
    r"^lakefile\.", r"^lake-manifest\.json$", r"^lean-toolchain$",
    r"^scripts/", r"^hooks/",
)


def merge_paths_allowed(paths) -> bool:
    """Whether every changed path is roadmap content the gate may merge.

    Deny wins over allow; anything matching neither list is denied — toolchain,
    CI, and tooling changes always wait for a human.
    """
    allow = [re.compile(p) for p in _MERGE_PATH_ALLOW]
    deny = [re.compile(p) for p in _MERGE_PATH_DENY]
    for path in paths:
        if any(rx.search(path) for rx in deny):
            return False
        if not any(rx.search(path) for rx in allow):
            return False
    return bool(list(paths))

# ---------------------------------------------------------------------------
# Claims (git-ref leases). Cooperative only — safety is CAS pushes.
# ---------------------------------------------------------------------------
CLAIM_REF_PREFIX = "refs/autoform-claims/"
CLAIM_SCHEMA = "autoform-claim/v1"
CLAIM_TTL_S = 1500          # lease lifetime
CLAIM_HEARTBEAT_S = 300     # renewal cadence while a unit runs
CLAIM_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")

# ---------------------------------------------------------------------------
# Round / loop timing.
# ---------------------------------------------------------------------------
ROUND_TIMEOUT_S = 5400      # hard wall for one round (process-group killed past it)
INTERROUND_S = 20           # pause between productive rounds
IDLE_POLL_S = 300           # pause after a no-progress round
BACKOFF_BASE_S = 30         # error backoff: BASE * 2^streak, capped
BACKOFF_MAX_S = 900
GH_MIN_BUDGET = 200         # wait when the REST core budget drops below this

# ---------------------------------------------------------------------------
# Attempt budgets (persisted per worker in counters.json). A provider-infra
# failure (5xx/429/transport) refunds the attempt instead of burning it.
# ---------------------------------------------------------------------------
MAX_FIX_ATTEMPTS = 3        # per PR head
MAX_CI_ATTEMPTS = 3         # per PR head
MAX_CI_PR_ATTEMPTS = 5      # per PR lifetime
MAX_REBASE_ATTEMPTS = 3     # per PR
MAX_REVIEW_ERRORS = 3       # per PR
MAX_INFRA_REFUNDS = 20      # per counter — a refund cap so outages still terminate

# ---------------------------------------------------------------------------
# GitHub markers — the machine-readable coordination state that lives on PRs.
# ---------------------------------------------------------------------------
SCOREBOARD_MARK = "<!--autoform-scoreboard-->"
META_MARK_RE = re.compile(r"<!--autoform-meta:v1\s+(\{.*?\})\s*-->", re.DOTALL)
TARGET_MARK_RE = re.compile(r"<!--autoform-target:v1\s+(\{.*?\})\s*-->", re.DOTALL)
INPROGRESS_MARK_RE = re.compile(
    r"<!--autoform-review-in-progress\s+(\{.*?\})\s*-->", re.DOTALL
)
REVIEW_INPROGRESS_TTL_S = 3600

ESCALATION_ISSUE_MARK_RE = re.compile(
    r"<!--autoform-escalation:v1\s+(\{.*?\})\s*-->", re.DOTALL
)

# Branch naming for authored work.
PROVE_BRANCH_PREFIX = "autoform/"
REVIEW_TMP_BRANCH_PREFIX = "autoform-review/"

# Best-effort PR/issue labels (creation failures are ignored).
LABEL_BASE = "autoform"
LABEL_ESCALATION = "autoform:escalation"
LABEL_INTENTION = "autoform:intention"
