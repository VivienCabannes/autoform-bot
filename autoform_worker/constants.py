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
STAGES = ("rebase", "fix-ci", "fix", "review", "progress", "prove")

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
MAX_PROVE_ATTEMPTS = 3      # per node
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
